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

"""Per-session throttling for after-turn fork callbacks.

Both ``auto_capture_callback`` and ``review_fork_callback`` fire on every
parent turn. Without throttling, a burst of short user turns means two
near-identical Memory Bank writes and two near-identical review-fork
spawns within a few seconds. The throttling layer enforces a per-type
cooldown (default 120s) and a per-session safety cap (50 runs).

Tests below patch ``app.memory._throttle.time.time`` so the clock is
deterministic — never assert on real wallclock behavior in tests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.memory import InMemoryMemoryService

from app.memory import _throttle


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(_throttle.time, "time", lambda: clock["now"])
    return clock


# =============================================================================
# Unit-level: try_claim
# =============================================================================


class TestTryClaim:
    def test_first_call_fires(self, fake_clock: dict[str, float]) -> None:
        state: dict[str, Any] = {}
        assert _throttle.try_claim(state, "auto_capture") is True

    def test_records_run_in_state(self, fake_clock: dict[str, float]) -> None:
        state: dict[str, Any] = {}
        _throttle.try_claim(state, "auto_capture")
        entry = state["_fork_throttle"]["auto_capture"]
        assert entry["count"] == 1
        assert entry["last_at"] == fake_clock["now"]

    def test_second_call_within_cooldown_skips(
        self, fake_clock: dict[str, float]
    ) -> None:
        state: dict[str, Any] = {}
        assert _throttle.try_claim(state, "auto_capture") is True
        fake_clock["now"] += 60.0  # still inside the 120s window
        assert _throttle.try_claim(state, "auto_capture") is False
        # State unchanged on skip.
        assert state["_fork_throttle"]["auto_capture"]["count"] == 1

    def test_call_after_cooldown_fires_again(
        self, fake_clock: dict[str, float]
    ) -> None:
        state: dict[str, Any] = {}
        _throttle.try_claim(state, "auto_capture")
        fake_clock["now"] += 130.0  # past the 120s window
        assert _throttle.try_claim(state, "auto_capture") is True
        assert state["_fork_throttle"]["auto_capture"]["count"] == 2

    def test_different_fork_types_are_independent(
        self, fake_clock: dict[str, float]
    ) -> None:
        state: dict[str, Any] = {}
        assert _throttle.try_claim(state, "auto_capture") is True
        # Same instant, different type — must not share the cooldown.
        assert _throttle.try_claim(state, "review_fork") is True
        assert state["_fork_throttle"]["auto_capture"]["count"] == 1
        assert state["_fork_throttle"]["review_fork"]["count"] == 1

    def test_per_session_cap(
        self,
        fake_clock: dict[str, float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Make the cap easy to drive.
        monkeypatch.setattr(_throttle, "_FORK_PER_SESSION_CAP", 3)
        state: dict[str, Any] = {}
        for i in range(3):
            fake_clock["now"] += 1000.0  # always past cooldown
            assert _throttle.try_claim(state, "auto_capture") is True, (
                f"run {i + 1} must be allowed under cap=3"
            )
        # 4th run hits the cap.
        fake_clock["now"] += 1000.0
        assert _throttle.try_claim(state, "auto_capture") is False
        assert state["_fork_throttle"]["auto_capture"]["count"] == 3

    def test_env_zero_disables_cooldown_but_keeps_cap(
        self,
        fake_clock: dict[str, float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LHA_FORK_COOLDOWN", "0")
        monkeypatch.setattr(_throttle, "_FORK_PER_SESSION_CAP", 3)
        state: dict[str, Any] = {}
        # Three back-to-back calls at the same instant — cooldown is off.
        for _ in range(3):
            assert _throttle.try_claim(state, "auto_capture") is True
        # Cap still applies.
        assert _throttle.try_claim(state, "auto_capture") is False

    def test_env_invalid_falls_back_to_default(
        self,
        fake_clock: dict[str, float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LHA_FORK_COOLDOWN", "not-a-number")
        state: dict[str, Any] = {}
        assert _throttle.try_claim(state, "auto_capture") is True
        assert (
            _throttle.try_claim(state, "auto_capture") is False
        )  # default 120s

    def test_state_is_none_passes_through(
        self, fake_clock: dict[str, float]
    ) -> None:
        # Callers without a session state surface must not crash.
        assert _throttle.try_claim(None, "auto_capture") is True

    def test_corrupt_state_entry_is_treated_as_fresh(
        self, fake_clock: dict[str, float]
    ) -> None:
        # If a previous version of the code wrote a non-dict, the throttle
        # must not blow up — it should treat the slot as empty and proceed.
        state: dict[str, Any] = {"_fork_throttle": "garbage"}
        assert _throttle.try_claim(state, "auto_capture") is True
        # And it should re-initialize the slot to a dict.
        assert isinstance(state["_fork_throttle"], dict)

    def test_corrupt_per_type_entry_is_treated_as_fresh(
        self, fake_clock: dict[str, float]
    ) -> None:
        # An outer dict but with a non-dict per-type entry (e.g. a string
        # written by an older version) must not raise AttributeError when
        # we go to read .get('count', ...).
        state: dict[str, Any] = {
            "_fork_throttle": {"auto_capture": "not_a_dict"}
        }
        assert _throttle.try_claim(state, "auto_capture") is True
        assert state["_fork_throttle"]["auto_capture"]["count"] == 1


# =============================================================================
# Wiring: auto_capture_callback
# =============================================================================


class _FakeInvocationContext:
    def __init__(self, memory_service: Any) -> None:
        self.memory_service = memory_service


class _FakeAutoCaptureContext:
    def __init__(self, memory_service: Any) -> None:
        self.state: dict[str, Any] = {}
        self._invocation_context = _FakeInvocationContext(memory_service)
        self.calls = 0

    async def add_session_to_memory(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
class TestAutoCaptureThrottle:
    async def test_first_turn_flushes(
        self, fake_clock: dict[str, float]
    ) -> None:
        from app.memory.auto_capture import auto_capture_callback

        ctx = _FakeAutoCaptureContext(memory_service=InMemoryMemoryService())
        await auto_capture_callback(ctx)  # type: ignore[arg-type]
        assert ctx.calls == 1

    async def test_back_to_back_turn_is_throttled(
        self, fake_clock: dict[str, float]
    ) -> None:
        from app.memory.auto_capture import auto_capture_callback

        ctx = _FakeAutoCaptureContext(memory_service=InMemoryMemoryService())
        await auto_capture_callback(ctx)  # type: ignore[arg-type]
        fake_clock["now"] += 5.0
        await auto_capture_callback(ctx)  # type: ignore[arg-type]
        assert ctx.calls == 1, "second turn must be skipped inside cooldown"

    async def test_post_cooldown_turn_flushes_again(
        self, fake_clock: dict[str, float]
    ) -> None:
        from app.memory.auto_capture import auto_capture_callback

        ctx = _FakeAutoCaptureContext(memory_service=InMemoryMemoryService())
        await auto_capture_callback(ctx)  # type: ignore[arg-type]
        fake_clock["now"] += 130.0
        await auto_capture_callback(ctx)  # type: ignore[arg-type]
        assert ctx.calls == 2


# =============================================================================
# Wiring: review_fork_callback
# =============================================================================


def _build_review_ctx(
    *, memory_service: Any, state: dict[str, Any] | None = None
) -> SimpleNamespace:
    session = SimpleNamespace(id="parent_session", events=[])
    return SimpleNamespace(
        state=dict(state or {}),
        _invocation_context=SimpleNamespace(
            memory_service=memory_service,
            app_name="parent_app",
            user_id="parent_user",
            session=session,
        ),
    )


@pytest.mark.asyncio
class TestReviewForkThrottle:
    async def test_back_to_back_callback_spawns_once(
        self,
        fake_clock: dict[str, float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.memory import review_fork

        spawned: list[Any] = []

        def fake_create_task(coro: Any) -> Any:
            spawned.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(asyncio, "create_task", fake_create_task)

        ctx = _build_review_ctx(memory_service=InMemoryMemoryService())
        await review_fork.review_fork_callback(ctx)
        # Reuse the same state, simulating two turns in the same session.
        fake_clock["now"] += 10.0
        await review_fork.review_fork_callback(ctx)
        assert len(spawned) == 1, "second within-cooldown turn must not spawn"
