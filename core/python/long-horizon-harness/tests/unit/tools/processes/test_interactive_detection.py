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

"""Tests for the interactive-prompt heuristic.

``detect_interactive_prompt(tail, idle_seconds)`` returns the matched
prompt fragment (or None) iff the process has been idle long enough AND
the output tail matches a known prompt pattern. False positives on
non-prompt output (`Done.`, log lines ending in `?` in the middle of
text) must NOT fire.
"""

from __future__ import annotations

import pytest


def _detect(tail, idle=5.0):
    """Convenience wrapper. Uses the default idle threshold > the default."""
    from app.tools.processes.interactive import (
        detect_interactive_prompt,
    )

    return detect_interactive_prompt(tail, idle_seconds=idle)


class TestPositiveMatches:
    @pytest.mark.parametrize(
        "tail",
        [
            b"Password: ",
            b"password:",
            b"PASSWORD: ",
            b"sudo password for sam: ",
            b"Enter passphrase: ",
        ],
    )
    def test_password_prompts(self, tail) -> None:
        assert _detect(tail) is not None

    @pytest.mark.parametrize(
        "tail",
        [
            b"Continue? [y/N] ",
            b"Are you sure? [Y/n]",
            b"Proceed (yes/no)? ",
            b"OK to install (Y/n)? ",
        ],
    )
    def test_yes_no_prompts(self, tail) -> None:
        assert _detect(tail) is not None

    @pytest.mark.parametrize(
        "tail",
        [b">>> ", b"In [3]: ", b"(Pdb) ", b"(Pdb)"],
    )
    def test_repl_prompts(self, tail) -> None:
        assert _detect(tail) is not None

    def test_match_includes_relevant_tail(self) -> None:
        match = _detect(b"some setup output\nPassword: ")
        assert match is not None
        assert "password" in match.lower()


class TestNegativeMatches:
    @pytest.mark.parametrize(
        "tail",
        [
            b"Done.\n",
            b"OK\n",
            b"Installed 3 packages.\n",
            b"compiled hello.c successfully\n",
            b"",
            b"\n",
            b"running tests... 12/30",
        ],
    )
    def test_normal_output_does_not_fire(self, tail) -> None:
        assert _detect(tail) is None

    def test_question_mark_mid_line_does_not_fire(self) -> None:
        # "What now? Looking at next steps." — the `?` is not at end of line.
        assert _detect(b"What now? Looking at next steps.") is None

    def test_lone_arrow_in_log_does_not_fire(self) -> None:
        # Arrow inside a log line is not a REPL prompt.
        assert _detect(b"forwarding 8080 -> 80\n") is None


class TestIdleGate:
    def test_too_recent_returns_none(self) -> None:
        # Even a textbook prompt is ignored if the process just emitted output.
        assert _detect(b"Password: ", idle=0.5) is None

    def test_threshold_inclusive(self) -> None:
        # Default detection threshold is 3.0s — match should fire at exactly 3.0.
        from app.tools.processes.interactive import (
            INTERACTIVE_IDLE_SECONDS,
            detect_interactive_prompt,
        )

        assert (
            detect_interactive_prompt(
                b"Password: ", idle_seconds=INTERACTIVE_IDLE_SECONDS
            )
            is not None
        )


class TestANSIStripping:
    def test_ansi_color_codes_dont_block_match(self) -> None:
        # \x1b[33mPassword: \x1b[0m — colorized prompt should still match.
        tail = b"\x1b[33mPassword: \x1b[0m"
        assert _detect(tail) is not None


class TestTailWindow:
    def test_only_last_kilobyte_inspected(self) -> None:
        # A prompt buried under 8KB of unrelated output (with the prompt
        # outside the inspection window) shouldn't fire. Inspection window
        # is the last 512 bytes per spec.
        leading_filler = b"x" * 10_000
        prompt_in_old_data = leading_filler + b"Password: " + (b"y" * 600)
        assert _detect(prompt_in_old_data) is None
