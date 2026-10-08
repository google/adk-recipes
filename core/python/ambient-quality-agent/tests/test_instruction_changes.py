# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for `TurnInstructionTracker`: a changed instruction shows once per turn."""

from __future__ import annotations

from ambient_quality_agent.tools.ingestion.instruction_changes import (
    SYSTEM_INSTRUCTION_KEY,
    TurnInstructionTracker,
)

BASE = "You are a helpful agent."
CHANGED = "You are a helpful agent. Today is Monday."


def test_instruction_equal_to_baseline_is_not_shown():
    """The agent block already shows the baseline, so events do not repeat it."""
    tracker = TurnInstructionTracker({"root": BASE})

    assert tracker.build_state_delta("root", "root", BASE) is None


def test_changed_instruction_is_shown_once():
    """A changed instruction shows on the first event, not on later ones."""
    tracker = TurnInstructionTracker({"root": BASE})

    assert tracker.build_state_delta("root", "root", CHANGED) == {
        SYSTEM_INSTRUCTION_KEY: CHANGED
    }
    assert tracker.build_state_delta("root", "root", CHANGED) is None


def test_change_back_to_baseline_is_shown():
    """Returning to the baseline after a change is itself a change."""
    tracker = TurnInstructionTracker({"root": BASE})
    tracker.build_state_delta("root", "root", CHANGED)

    assert tracker.build_state_delta("root", "root", BASE) == {
        SYSTEM_INSTRUCTION_KEY: BASE
    }
    assert tracker.build_state_delta("root", "root", BASE) is None


def test_other_author_does_not_count_as_shown():
    """User and other-agent events are skipped; the agent's own event shows it."""
    tracker = TurnInstructionTracker({"root": BASE})

    assert tracker.build_state_delta("root", "user", CHANGED) is None
    assert tracker.build_state_delta("root", "helper", CHANGED) is None
    assert tracker.build_state_delta("root", "root", CHANGED) == {
        SYSTEM_INSTRUCTION_KEY: CHANGED
    }


def test_missing_agent_or_instruction_records_nothing():
    """An unknown agent or instruction returns None and records nothing."""
    tracker = TurnInstructionTracker({"root": BASE})

    assert tracker.build_state_delta(None, None, CHANGED) is None
    assert tracker.build_state_delta("", "", CHANGED) is None
    assert tracker.build_state_delta("root", "root", None) is None
    assert tracker.build_state_delta("root", "root", CHANGED) == {
        SYSTEM_INSTRUCTION_KEY: CHANGED
    }


def test_agents_are_tracked_independently():
    """Showing one agent's instruction does not affect another agent."""
    tracker = TurnInstructionTracker({"root": BASE, "helper": BASE})

    assert tracker.build_state_delta("root", "root", CHANGED) == {
        SYSTEM_INSTRUCTION_KEY: CHANGED
    }
    assert tracker.build_state_delta("helper", "helper", CHANGED) == {
        SYSTEM_INSTRUCTION_KEY: CHANGED
    }
    assert tracker.build_state_delta("helper", "helper", BASE) == {
        SYSTEM_INSTRUCTION_KEY: BASE
    }
    assert tracker.build_state_delta("root", "root", CHANGED) is None


def test_new_turn_shows_active_instruction_again():
    """Each turn reads on its own, so a new tracker shows the change again."""
    baselines = {"root": BASE}
    first_turn = TurnInstructionTracker(baselines)
    first_turn.build_state_delta("root", "root", CHANGED)

    second_turn = TurnInstructionTracker(baselines)

    assert second_turn.build_state_delta("root", "root", CHANGED) == {
        SYSTEM_INSTRUCTION_KEY: CHANGED
    }


def test_baseline_added_after_construction_is_honored():
    """The baselines mapping is read at call time, not copied at construction."""
    baselines: dict[str, str | None] = {}
    tracker = TurnInstructionTracker(baselines)
    baselines["root"] = BASE

    assert tracker.build_state_delta("root", "root", BASE) is None


def test_agent_without_baseline_shows_instruction():
    """With no recorded baseline, the first known instruction is a change."""
    tracker = TurnInstructionTracker({})

    assert tracker.build_state_delta("root", "root", BASE) == {
        SYSTEM_INSTRUCTION_KEY: BASE
    }
    assert tracker.build_state_delta("root", "root", BASE) is None


def test_explicit_none_baseline_shows_instruction():
    """A baseline recorded as None means any known instruction is a change."""
    tracker = TurnInstructionTracker({"root": None})

    assert tracker.build_state_delta("root", "root", BASE) == {
        SYSTEM_INSTRUCTION_KEY: BASE
    }
