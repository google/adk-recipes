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

"""DispatchingLlm routes generate_content_async to the registered backend
matching ``llm_request.model`` and applies each model's optional
``prepare_contents`` hook (the per-model content interface)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from app.models.capabilities import ModelCapabilities

pytestmark = pytest.mark.asyncio


class _FakeLlm(BaseLlm):
    """Echo backend that records the request model it was called with."""

    model: str
    label: str

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"{self.label}:{llm_request.model}")],
            )
        )


class _CapturingLlm(BaseLlm):
    """Records the contents it received, so hook transforms are observable."""

    model: str
    seen: list[Any] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.seen.append(list(llm_request.contents or []))
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="ok")])
        )


def _backends() -> dict[str, BaseLlm]:
    return {
        "alpha": _FakeLlm(model="alpha", label="A"),
        "beta": _FakeLlm(model="beta", label="B"),
    }


async def _collect(agen: AsyncGenerator[Any, None]) -> list[Any]:
    return [x async for x in agen]


async def test_dispatches_to_named_backend():
    from app.models.dispatcher import DispatchingLlm

    llm = DispatchingLlm(model="alpha", backends=_backends())
    req = LlmRequest(model="beta")
    [resp] = await _collect(llm.generate_content_async(req))
    assert resp.content.parts[0].text == "B:beta"


async def test_falls_back_to_default_when_request_model_unset():
    from app.models.dispatcher import DispatchingLlm

    llm = DispatchingLlm(model="alpha", backends=_backends())
    req = LlmRequest()  # no model
    [resp] = await _collect(llm.generate_content_async(req))
    assert resp.content.parts[0].text == "A:alpha"


async def test_unknown_model_raises_with_known_names_in_message():
    from app.models.dispatcher import DispatchingLlm

    llm = DispatchingLlm(model="alpha", backends=_backends())
    req = LlmRequest(model="gamma")
    with pytest.raises(ValueError, match="alpha"):
        await _collect(llm.generate_content_async(req))


async def test_default_must_exist_in_backends():
    from app.models.dispatcher import DispatchingLlm

    with pytest.raises(ValueError):
        DispatchingLlm(model="missing", backends=_backends())


def _caps_with(hook) -> ModelCapabilities:
    return ModelCapabilities(
        max_image_bytes=None,
        can_view_mime=lambda _m: True,
        prepare_contents=hook,
    )


async def test_prepare_contents_hook_transforms_before_dispatch(monkeypatch):
    """A model whose capabilities set prepare_contents has its contents
    transformed before the backend sees them — the per-model content interface."""
    from app.models import dispatcher as disp_mod
    from app.models.dispatcher import DispatchingLlm

    def _hook(contents: list[types.Content]) -> list[types.Content]:
        return [
            types.Content(role="user", parts=[types.Part(text="TRANSFORMED")])
        ]

    monkeypatch.setattr(
        disp_mod, "model_capabilities", lambda _n: _caps_with(_hook)
    )
    backend = _CapturingLlm(model="alpha")
    llm = DispatchingLlm(model="alpha", backends={"alpha": backend})
    req = LlmRequest(
        model="alpha",
        contents=[types.Content(role="user", parts=[types.Part(text="orig")])],
    )
    await _collect(llm.generate_content_async(req))
    assert backend.seen[0][0].parts[0].text == "TRANSFORMED"


async def test_no_hook_passes_contents_through_untouched(monkeypatch):
    from app.models import dispatcher as disp_mod
    from app.models.dispatcher import DispatchingLlm

    monkeypatch.setattr(
        disp_mod, "model_capabilities", lambda _n: _caps_with(None)
    )
    backend = _CapturingLlm(model="alpha")
    llm = DispatchingLlm(model="alpha", backends={"alpha": backend})
    orig = [types.Content(role="user", parts=[types.Part(text="orig")])]
    req = LlmRequest(model="alpha", contents=orig)
    await _collect(llm.generate_content_async(req))
    assert backend.seen[0][0].parts[0].text == "orig"
