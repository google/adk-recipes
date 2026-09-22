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

"""Tests for ``HorizonSummarizer`` — banner wrapping + pre-compaction flush fork.

The summarizer's LLM is stubbed so these tests drive the real override
end-to-end without hitting Gemini. Real LLM behavior lives in evals.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.events.event import Event
from google.genai.types import Content, Part

from horizon.context.compaction_context import (
    CompactionContext,
    bind_compaction_context,
    clear_compaction_context,
)
from horizon.context.summarizer import (
    SUMMARY_BANNER_PREFIX,
    HorizonSummarizer,
)

pytestmark = pytest.mark.asyncio


def _user_event(text: str, *, ts: float = 1.0) -> Event:
    return Event(
        author="user",
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id=f"test-{ts}",
        timestamp=ts,
    )


def _build_summarizer(
    summary_text: str | None = "structured summary",
    *,
    usage_metadata: Any = None,
) -> HorizonSummarizer:
    class _StubLlm:
        model = "stub"

        async def generate_content_async(self, request, stream=False):
            from google.adk.models.llm_response import LlmResponse

            if summary_text is None:
                return
            yield LlmResponse(
                content=Content(role="model", parts=[Part(text=summary_text)]),
                usage_metadata=usage_metadata,
            )

    return HorizonSummarizer(llm=_StubLlm())  # type: ignore[arg-type]


@pytest.fixture()
def _scoped_compaction_context() -> Any:
    clear_compaction_context()
    yield
    clear_compaction_context()


# =========================================================================
# Banner wrapping
# =========================================================================


async def test_banner_prefixed_to_compacted_content(
    _scoped_compaction_context: None,
):
    summarizer = _build_summarizer("middle was about auth")

    result = await summarizer.maybe_summarize_events(
        events=[_user_event("hello")]
    )

    assert result is not None
    assert result.actions is not None
    assert result.actions.compaction is not None
    text = result.actions.compaction.compacted_content.parts[0].text
    assert text is not None
    assert text.startswith(SUMMARY_BANNER_PREFIX)
    assert "middle was about auth" in text


async def test_compaction_event_carries_usage_metadata(
    _scoped_compaction_context: None,
):
    from google.genai.types import GenerateContentResponseUsageMetadata

    usage = GenerateContentResponseUsageMetadata(total_token_count=321)
    summarizer = _build_summarizer("summary", usage_metadata=usage)

    result = await summarizer.maybe_summarize_events(
        events=[_user_event("hello")]
    )

    assert result is not None
    assert result.usage_metadata is usage
    assert result.usage_metadata.total_token_count == 321


async def test_no_banner_when_llm_returns_nothing(
    _scoped_compaction_context: None,
):
    summarizer = _build_summarizer(summary_text=None)

    result = await summarizer.maybe_summarize_events(events=[_user_event("hi")])

    assert result is None


async def test_empty_events_returns_none(
    _scoped_compaction_context: None,
):
    summarizer = _build_summarizer()

    result = await summarizer.maybe_summarize_events(events=[])

    assert result is None


# =========================================================================
# Pre-compaction flush fork
# =========================================================================


@pytest.fixture()
def captured_flush(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"called": False, "kwargs": None}

    def _fake_spawn_flush_fork(**kwargs: Any) -> bool:
        captured["called"] = True
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(
        "horizon.context.summarizer.spawn_flush_fork",
        _fake_spawn_flush_fork,
    )
    return captured


async def test_flush_fork_spawned_when_context_present(
    captured_flush: dict[str, Any],
    _scoped_compaction_context: None,
):
    memory_service = SimpleNamespace()
    bind_compaction_context(
        CompactionContext(
            memory_service=memory_service,
            app_name="lha-test",
            user_id="user-42",
        )
    )
    summarizer = _build_summarizer()
    events = [_user_event("hello"), _user_event("world", ts=2.0)]

    await summarizer.maybe_summarize_events(events=events)

    assert captured_flush["called"] is True
    kwargs = captured_flush["kwargs"]
    assert kwargs["parent_memory_service"] is memory_service
    assert kwargs["parent_app_name"] == "lha-test"
    assert kwargs["parent_user_id"] == "user-42"
    assert kwargs["events"] == events


async def test_flush_fork_skipped_when_context_missing(
    captured_flush: dict[str, Any],
    _scoped_compaction_context: None,
):
    summarizer = _build_summarizer()

    result = await summarizer.maybe_summarize_events(events=[_user_event("hi")])

    assert captured_flush["called"] is False
    assert result is not None


async def test_flush_fork_failure_does_not_block_compaction(
    monkeypatch: pytest.MonkeyPatch,
    _scoped_compaction_context: None,
):
    def _boom(**_kwargs: Any) -> bool:
        raise RuntimeError("flush spawn exploded")

    monkeypatch.setattr("horizon.context.summarizer.spawn_flush_fork", _boom)
    bind_compaction_context(
        CompactionContext(
            memory_service=SimpleNamespace(),
            app_name="lha-test",
            user_id="user-42",
        )
    )
    summarizer = _build_summarizer()

    result = await summarizer.maybe_summarize_events(events=[_user_event("hi")])

    assert result is not None
    assert result.actions.compaction is not None


# =========================================================================
# Bounding the compaction prompt itself (the input, not the output)
#
# _format_history previously inlined every tool result and call verbatim,
# so the compaction prompt contained the full text of exactly the content
# that made compaction necessary in the first place: a second expensive
# LLM call that can itself approach the window. Tool results are capped
# at 2,000 chars during summarization serialization for that reason.
# =========================================================================


def _tool_result_event(name: str, body: str, *, ts: float = 1.0) -> Event:
    from google.genai.types import FunctionResponse

    return Event(
        author="user",
        content=Content(
            role="user",
            parts=[
                Part(
                    function_response=FunctionResponse(
                        id=f"{name}-{ts}", name=name, response={"output": body}
                    )
                )
            ],
        ),
        invocation_id=f"tool-{ts}",
        timestamp=ts,
    )


def _tool_call_event(
    name: str, args: dict[str, Any], *, ts: float = 1.0
) -> Event:
    from google.genai.types import FunctionCall

    return Event(
        author="root_agent",
        content=Content(
            role="model",
            parts=[Part(function_call=FunctionCall(name=name, args=args))],
        ),
        invocation_id=f"call-{ts}",
        timestamp=ts,
    )


def test_huge_tool_result_is_capped_in_history():
    from horizon.context.summarizer import _format_history

    events = [_tool_result_event("bash", "x" * 50_000)]

    history = _format_history(events)

    assert len(history) < 3_000, (
        f"a single 50,000-char tool result produced a {len(history)}-char "
        "compaction-prompt line; it must be capped, not inlined verbatim."
    )
    assert "chars omitted" in history


def test_huge_function_call_args_are_capped_in_history():
    from horizon.context.summarizer import _format_history

    events = [
        _tool_call_event("write", {"content": "y" * 50_000, "path": "f.txt"})
    ]

    history = _format_history(events)

    assert len(history) < 3_000
    assert "chars omitted" in history


def test_small_tool_result_is_not_marked_or_altered():
    from horizon.context.summarizer import _format_history

    events = [_tool_result_event("read", "small output")]

    history = _format_history(events)

    assert "small output" in history
    assert "chars omitted" not in history


def test_compaction_prompt_stays_bounded_with_many_large_tool_results():
    from horizon.context.summarizer import build_compaction_prompt

    events = [_tool_result_event("bash", "z" * 50_000, ts=i) for i in range(20)]

    prompt = build_compaction_prompt(events)

    # 20 x 50,000 chars uncapped would be ~1,000,000 chars — itself capable
    # of overflowing the model that is supposed to summarize it away.
    assert len(prompt) < 60_000, (
        f"compaction prompt is {len(prompt)} chars for 20 large tool "
        "results; per-item capping in _format_history did not apply."
    )


# =========================================================================
# 6c. Cumulative file tracking across compactions
#
# The LLM's own freeform "## Relevant Files" section can drop a
# still-relevant path on a lossy merge pass. This deterministically walks
# function_call args so tracking survives regardless of summarization
# quality, extracting file operations from both the summarized messages
# and the previous summary on every pass.
# =========================================================================


def _file_call_event(name: str, path: str, *, ts: float = 1.0) -> Event:
    from google.genai.types import FunctionCall

    return Event(
        author="root_agent",
        content=Content(
            role="model",
            parts=[
                Part(function_call=FunctionCall(name=name, args={"path": path}))
            ],
        ),
        invocation_id=f"filecall-{ts}",
        timestamp=ts,
    )


def _tracked_files_section(prompt: str) -> str:
    marker = "Files touched"
    idx = prompt.find(marker)
    assert idx != -1, f"no tracked-files section in prompt: {prompt!r}"
    return prompt[idx : idx + 500]


def test_touched_files_from_this_pass_are_listed():
    from horizon.context.summarizer import build_compaction_prompt

    events = [
        _file_call_event("read", "src/app.py", ts=1.0),
        _file_call_event("edit", "src/config.py", ts=2.0),
    ]

    prompt = build_compaction_prompt(events)
    section = _tracked_files_section(prompt)

    assert "src/app.py" in section
    assert "src/config.py (modified)" in section


def test_files_from_previous_summary_survive_a_second_pass_even_when_not_reread():
    from horizon.context.summarizer import (
        SUMMARY_BANNER_PREFIX,
        build_compaction_prompt,
    )

    # A previous summary whose "Relevant Files" section names a path that
    # is NOT touched again in this pass's own events. The verbatim
    # <previous-summary> block already reproduces this text (pre-existing
    # behavior, not what's under test here) — the assertion is scoped to
    # the NEW deterministic tracked-files section specifically, which must
    # re-surface it independent of the LLM's own freeform merge.
    previous_summary_event = Event(
        author="user",
        content=Content(
            role="user",
            parts=[
                Part(
                    text=(
                        f"{SUMMARY_BANNER_PREFIX}\n\n"
                        "## Relevant Files\n"
                        "- src/legacy.py: still in use\n"
                    )
                )
            ],
        ),
        invocation_id="seed",
        timestamp=0.0,
    )
    events = [
        previous_summary_event,
        _file_call_event("read", "src/new.py", ts=1.0),
    ]

    prompt = build_compaction_prompt(events)
    section = _tracked_files_section(prompt)

    assert "src/legacy.py" in section
    assert "src/new.py" in section


def test_no_files_touched_adds_no_section():
    from horizon.context.summarizer import build_compaction_prompt

    prompt = build_compaction_prompt([_user_event("just chatting")])

    assert "Files touched" not in prompt
