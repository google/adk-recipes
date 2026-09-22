# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Registry of root-agent model backends.

Each entry is a ``ModelDescriptor`` (how to build the ``BaseLlm``, its context
window, and its ``ModelCapabilities``) keyed by the name users type into
``/model``. ``DispatchingLlm`` routes each call to the named backend.

Adding a model is one entry in ``_MODELS``. Backends build lazily on first
access, so ``import horizon`` needs no GCP credentials and the ADC project probe
only runs when a backend that needs it is built. A model with unusual media
limits or content quirks sets its own ``ModelCapabilities`` — the dispatcher and
the ``read`` tool's media path stay backend-agnostic.
"""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from google.adk.models import Gemini
from google.adk.models.base_llm import BaseLlm
from google.genai import types

from horizon.models.capabilities import (
    DEFAULT_CAPABILITIES,
    GEMINI_CAPABILITIES,
    ModelCapabilities,
)

if TYPE_CHECKING:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

logger = logging.getLogger(__name__)


# http_status_codes left unset inherits the SDK's default transient set
# (408, 429, 500, 502, 503, 504). attempts=6 (~30s of exponential backoff)
# rides out a transient per-minute quota spike; sustained exhaustion still
# needs a quota bump. Reused by the Gemini subagents so retry is uniform.
ROBUST_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=6,
    initial_delay=1.0,
    max_delay=60.0,
    exp_base=2.0,
    jitter=1.0,
)


# Vertex's ServiceTier proto expects the SERVICE_TIER_* name, but google-genai's
# ServiceTier enum only carries the Developer-API spelling ('priority'). The
# Vertex literal routes through CaseInSensitiveEnum._missing_, yielding a member
# whose .value is that literal — so it serializes verbatim on the wire. Guarded
# so a future SDK that makes _missing_ raise degrades the gemini path to
# on-demand instead of failing this module's import.
def _vertex_priority_tier() -> types.ServiceTier | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return types.ServiceTier("SERVICE_TIER_PRIORITY")
    except Exception:
        return None


_PRIORITY_SERVICE_TIER = _vertex_priority_tier()


class _PriorityGemini(Gemini):
    """ADK Gemini pinned to Vertex's priority service tier."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if _PRIORITY_SERVICE_TIER is not None:
            llm_request.config.service_tier = _PRIORITY_SERVICE_TIER
        async for resp in super().generate_content_async(llm_request, stream):
            yield resp


def _priority_tier_enabled() -> bool:
    # Off by default: the priority tier needs a Vertex entitlement most projects
    # lack, so opt in explicitly with LHA_VERTEX_SERVICE_TIER=priority.
    return (
        os.environ.get("LHA_VERTEX_SERVICE_TIER", "").strip().lower()
        == "priority"
    )


def _build_gemini(model_id: str = "gemini-3.7-flash") -> Gemini:
    cls = _PriorityGemini if _priority_tier_enabled() else Gemini
    return cls(
        model=model_id,
        retry_options=ROBUST_RETRY_OPTIONS,
    )


DEFAULT_MODEL_NAME: str = "gemini-3.7-flash"
DEFAULT_INPUT_TOKEN_LIMIT: int = 200_000


@dataclass(frozen=True)
class ModelDescriptor:
    """One model's whole story: how to build it, its window, its capabilities."""

    build: Callable[[], BaseLlm]
    input_token_limit: int
    capabilities: ModelCapabilities


# Single source of truth per model. Add a backend = add one entry; capabilities
# carry the media limits + optional sanitize hook, so neither the dispatcher nor
# the read tool's media path names a concrete backend class. Input-token limits
# are conservative published context windows (under-estimating only fires
# compaction slightly earlier, which is safe).
_MODELS: dict[str, ModelDescriptor] = {
    "gemini-3.7-flash": ModelDescriptor(
        build=lambda: _build_gemini("gemini-3.7-flash"),
        input_token_limit=1_000_000,
        capabilities=GEMINI_CAPABILITIES,
    ),
    "gemini-3.1-pro-preview": ModelDescriptor(
        build=lambda: _build_gemini("gemini-3.1-pro-preview"),
        input_token_limit=1_000_000,
        capabilities=GEMINI_CAPABILITIES,
    ),
}


class _LazyModelRegistry(Mapping[str, BaseLlm]):
    """Builds each backend on first access and caches it, so importing the
    registry stays offline; membership/iteration are key-only."""

    def __init__(self, builders: dict[str, Callable[[], BaseLlm]]) -> None:
        self._builders = builders
        self._cache: dict[str, BaseLlm] = {}

    def __getitem__(self, name: str) -> BaseLlm:
        if name not in self._builders:
            raise KeyError(name)
        if name not in self._cache:
            self._cache[name] = self._builders[name]()
        return self._cache[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._builders)

    def __len__(self) -> int:
        return len(self._builders)

    def __contains__(self, name: object) -> bool:
        return name in self._builders


MODEL_REGISTRY: Mapping[str, BaseLlm] = _LazyModelRegistry(
    {name: d.build for name, d in _MODELS.items()}
)


def input_token_limit(name: str | None) -> int:
    """Context window for the compaction threshold; default when unknown."""
    descriptor = _MODELS.get(name) if name else None
    return (
        descriptor.input_token_limit
        if descriptor
        else DEFAULT_INPUT_TOKEN_LIMIT
    )


def model_capabilities(name: str | None) -> ModelCapabilities:
    """Backend capabilities (media limits + sanitize hook) for a model name.

    An unknown / directly-injected backend gets the safe passthrough default —
    so a well-behaved BaseLlm needs no dispatcher edit, and a model with unusual
    media limits or content quirks adds a descriptor with its own capabilities.
    """
    descriptor = _MODELS.get(name) if name else None
    return descriptor.capabilities if descriptor else DEFAULT_CAPABILITIES
