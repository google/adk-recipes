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

"""Tests for the memory guard."""

from __future__ import annotations

import pytest
from ambient_quality_agent.core import _memory


def test_memory_summary_reports_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_memory, "read_cgroup_current_mb", lambda: 1500.0)
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: 4096.0)
    assert _memory.render_memory_summary() == "cgroup 1500 MB/4096 MB"


def test_memory_summary_unavailable_when_no_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_memory, "read_cgroup_current_mb", lambda: None)
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: None)
    assert _memory.render_memory_summary() == "unavailable"


def test_cgroup_limit_ignores_unlimited_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 'unlimited' near-max value is reported as no limit (None)."""
    monkeypatch.setattr(_memory, "_read_int", lambda _p: 1 << 63)
    assert _memory.read_cgroup_limit_mb() is None


def test_memory_guard_reserves_for_growth_doubling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Does not stop while the projected peak stays under the limit."""
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: 4096.0)
    monkeypatch.setattr(_memory, "read_cgroup_current_mb", lambda: 1700.0)

    # baseline=1000 injected; current=1700, growth=700 ->
    # 1700+700+400=2800 < 4096 -> False.
    guard = _memory.MemoryGuard(reserve_mb=400, baseline_mb=1000.0)
    assert guard.should_stop() is False


def test_memory_guard_stops_when_projected_peak_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: 4096.0)
    monkeypatch.setattr(_memory, "read_cgroup_current_mb", lambda: 3000.0)

    # baseline=1000 injected; current=3000, growth=2000 ->
    # 3000+2000+400=5400 >= 4096 -> True.
    guard = _memory.MemoryGuard(reserve_mb=400, baseline_mb=1000.0)
    assert guard.should_stop() is True


def test_injected_baseline_is_shared_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both scopes' guards, seeded with the run baseline, agree on growth.

    The single-turn scope builds a distinct guard at already-elevated memory;
    with the injected run baseline it must NOT re-baseline there, so both
    guards project the same peak and reach the same decision.
    """
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: 4096.0)
    monkeypatch.setattr(_memory, "read_cgroup_current_mb", lambda: 3000.0)

    multi_turn = _memory.MemoryGuard(reserve_mb=400, baseline_mb=1000.0)
    single_turn = _memory.MemoryGuard(reserve_mb=400, baseline_mb=1000.0)

    # growth=2000 -> 3000+2000+400=5400 >= 4096 -> True for both.
    assert multi_turn.should_stop() is True
    assert single_turn.should_stop() is True


def test_memory_guard_lazily_captures_baseline_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no injected baseline, the first reading becomes the baseline."""
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: 4096.0)
    readings = iter([3000.0, 3000.0])
    monkeypatch.setattr(
        _memory, "read_cgroup_current_mb", lambda: next(readings)
    )

    guard = _memory.MemoryGuard(reserve_mb=400)
    # First call baselines at 3000, growth=0 -> 3000+0+400 < 4096 -> False.
    assert guard.should_stop() is False


def test_memory_guard_never_fires_when_cgroup_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_memory, "read_cgroup_current_mb", lambda: None)
    monkeypatch.setattr(_memory, "read_cgroup_limit_mb", lambda: None)
    assert _memory.MemoryGuard().should_stop() is False
