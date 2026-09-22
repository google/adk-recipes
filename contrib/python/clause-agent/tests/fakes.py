"""A minimal, public-API-only fake `BaseLlm` for deterministic tests.

`google-adk`'s own `MockModel`/`tests.unittests.testing_utils` (referenced
by the adk-agent-builder skill's `testing.md`) live in the adk-python
repo's *internal* test suite -- they are not shipped in the installed
`google-adk` PyPI package, so importing them from a downstream consumer
project fails (verified: `find ... -iname "*testing_utils*"` in the
installed package comes up empty). This subclasses the public, documented
`BaseLlm` interface (`google.adk.models.base_llm.BaseLlm`) instead, which
*is* part of the installed package and is exactly what `LiteLlm` and every
real model integration subclass.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr


class ScriptedModel(BaseLlm):
    """Yields pre-scripted responses in order, one per model call.

    `responses` is a flat queue of `list[types.Part]`; each call to
    `generate_content_async` pops the next entry. When the SAME instance
    is passed as `model=` to every agent in a tree (root + sub-agents),
    `responses` represents the full, ordered sequence of model decisions
    across the whole scripted conversation, regardless of which agent is
    "thinking" at each step.
    """

    model: str = "scripted-fake-model"
    _responses: list[list[types.Part]] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: list[list[types.Part]], **kwargs):
        super().__init__(**kwargs)
        self._responses = responses
        self._index = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self._index >= len(self._responses):
            raise AssertionError(
                "ScriptedModel ran out of scripted responses at call"
                f" #{self._index + 1}."
            )
        parts = self._responses[self._index]
        self._index += 1
        yield LlmResponse(content=types.Content(parts=parts, role="model"))


def text(t: str) -> list[types.Part]:
    """Shorthand for a text-only scripted response."""
    return [types.Part.from_text(text=t)]


def call(name: str, **args) -> list[types.Part]:
    """Shorthand for a single-function-call scripted response."""
    return [types.Part.from_function_call(name=name, args=args)]


def text_and_call(t: str, name: str, **args) -> list[types.Part]:
    """Shorthand for a text + function-call scripted response."""
    return [
        types.Part.from_text(text=t),
        types.Part.from_function_call(name=name, args=args),
    ]
