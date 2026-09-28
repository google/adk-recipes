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

"""Deterministic tests for the structured + anchored compaction prompt.

Asserts on the *prompt string* lha hands the summarizer LLM — never on the
LLM's summary. The 7-section template and the anchored-merge wrapping are pure
string construction and fully testable offline.
"""

from __future__ import annotations

from google.adk.events.event import Event
from google.genai.types import Content, Part

from app.context.summarizer import (
    SUMMARY_BANNER_PREFIX,
    build_compaction_prompt,
)


def _user(text: str, *, ts: float = 1.0) -> Event:
    return Event(
        author="user",
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id=f"u-{ts}",
        timestamp=ts,
    )


def _prior_summary_seed(body: str, *, ts: float = 0.5) -> Event:
    # Mirrors ADK's seed: author='model', content == prior compacted_content,
    # which (because HorizonSummarizer prepends it) starts with the banner.
    text = f"{SUMMARY_BANNER_PREFIX}\n\n{body}"
    return Event(
        author="model",
        content=Content(role="model", parts=[Part(text=text)]),
        invocation_id="seed",
        timestamp=ts,
    )


class TestTemplateSections:
    def test_contains_all_seven_sections_in_order(self) -> None:
        prompt = build_compaction_prompt([_user("hi")])
        order = [
            "## Goal",
            "## Constraints & Preferences",
            "## Progress",
            "### Done",
            "### In Progress",
            "### Blocked",
            "## Key Decisions",
            "## Next Steps",
            "## Critical Context",
            "## Relevant Files",
        ]
        last = -1
        for marker in order:
            pos = prompt.find(marker)
            assert pos != -1, f"missing section: {marker}"
            assert pos > last, f"section out of order: {marker}"
            last = pos

    def test_preservation_rules_present(self) -> None:
        prompt = build_compaction_prompt([_user("hi")])
        assert "exact file paths" in prompt
        assert "commands" in prompt
        assert "error strings" in prompt
        assert "identifiers" in prompt

    def test_includes_conversation_history(self) -> None:
        prompt = build_compaction_prompt([_user("deploy the thing")])
        assert "deploy the thing" in prompt


class TestAnchoredMerge:
    def test_no_previous_summary_block_when_no_seed(self) -> None:
        prompt = build_compaction_prompt([_user("hello")])
        assert "<previous-summary>" not in prompt

    def test_seed_is_wrapped_in_previous_summary_block(self) -> None:
        seed = _prior_summary_seed("## Goal\n- ship feature X")
        prompt = build_compaction_prompt([seed, _user("now do Y")])
        assert "<previous-summary>" in prompt
        assert "</previous-summary>" in prompt
        assert "ship feature X" in prompt

    def test_seed_banner_stripped_from_previous_summary_body(self) -> None:
        seed = _prior_summary_seed("## Goal\n- ship feature X")
        prompt = build_compaction_prompt([seed, _user("now do Y")])
        # The banner itself should not be re-fed inside the merge block.
        assert SUMMARY_BANNER_PREFIX not in prompt

    def test_seed_excluded_from_new_history(self) -> None:
        seed = _prior_summary_seed("OLD-SUMMARY-MARKER")
        prompt = build_compaction_prompt([seed, _user("NEW-TURN-MARKER")])
        # New history section has the new turn but not the old summary text
        # (which only appears inside <previous-summary>).
        _, _, after = prompt.partition("</previous-summary>")
        assert "NEW-TURN-MARKER" in after
        assert "OLD-SUMMARY-MARKER" not in after

    def test_merge_instructions_present_when_seed(self) -> None:
        seed = _prior_summary_seed("x")
        prompt = build_compaction_prompt([seed, _user("y")])
        assert "preserve" in prompt.lower()
        assert "stale" in prompt.lower()
        assert "merge" in prompt.lower()
