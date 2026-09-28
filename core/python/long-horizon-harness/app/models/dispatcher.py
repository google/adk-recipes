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

"""``DispatchingLlm`` — a ``BaseLlm`` that owns one or more backend clients and
routes each call to the one named in ``llm_request.model``.

ADK binds the underlying SDK client when the agent's model is resolved, which
happens once per agent. To support switching at session granularity (via
``/model``), the agent's ``model=`` is set to a single ``DispatchingLlm``
instance that holds every registered backend and picks per call. Adding a model
is one entry in ``app/models/registry.py``; the dispatcher itself is
backend-agnostic (it reads each model's ``ModelCapabilities`` by data).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from typing import TYPE_CHECKING, Any

from google.adk.models.base_llm import BaseLlm
from pydantic import ConfigDict, SkipValidation, model_validator

from app.models.registry import model_capabilities

if TYPE_CHECKING:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

logger = logging.getLogger(__name__)


class DispatchingLlm(BaseLlm):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # SkipValidation: pydantic would coerce a Mapping into a dict, iterating
    # every value and materializing each backend. Storing the lazy registry
    # as-is keeps an unused backend unbuilt until first use.
    backends: SkipValidation[Mapping[str, BaseLlm]]

    @model_validator(mode="after")
    def _default_must_be_known(self) -> DispatchingLlm:
        if self.model not in self.backends:
            raise ValueError(
                f"default model {self.model!r} not in backends "
                f"({sorted(self.backends)})"
            )
        return self

    @classmethod
    def supported_models(cls) -> list[str]:
        # Used only as an instance on Agent.model — never resolved via LlmRegistry.
        return []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        name = llm_request.model or self.model
        backend = self.backends.get(name)
        if backend is None:
            raise ValueError(
                f"unknown model {name!r}; available: {sorted(self.backends)}"
            )
        llm_request.model = name
        caps = model_capabilities(name)
        if caps.prepare_contents is not None and llm_request.contents:
            llm_request.contents = caps.prepare_contents(llm_request.contents)
        async for resp in backend.generate_content_async(llm_request, stream):
            yield resp

    def connect(self, llm_request: Any) -> Any:
        name = getattr(llm_request, "model", None) or self.model
        backend = self.backends.get(name)
        if backend is None:
            raise ValueError(
                f"unknown model {name!r}; available: {sorted(self.backends)}"
            )
        return backend.connect(llm_request)
