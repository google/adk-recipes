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

"""Tests for retrying a metric on transient failures."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from ambient_quality_agent.tools import transient_retry as retry
from ambient_quality_agent.tools.transient_retry import (
    MAX_TRIES,
    wrap_with_transient_retry,
)


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits instead of serving them, so the curve can be asserted."""
    waits: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full jitter picks in [0, ceiling); pin it to the ceiling to assert it."""
    monkeypatch.setattr(retry.random, "uniform", lambda _low, high: high)


def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    calls: list[Any] = []

    def flaky(instance: Any) -> dict:
        calls.append(instance)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return {"score": 1.0}

    assert wrap_with_transient_retry(flaky)({"x": 1}) == {"score": 1.0}
    assert len(calls) == 3


def test_a_permanent_failure_is_raised_at_once() -> None:
    calls: list[Any] = []

    def broken(instance: Any) -> dict:
        calls.append(instance)
        raise ValueError("bad metric")

    with pytest.raises(ValueError, match="bad metric"):
        wrap_with_transient_retry(broken)({})

    assert len(calls) == 1, "a bug in the metric must not be retried"


def test_it_gives_up_after_max_tries() -> None:
    calls: list[Any] = []

    def always_flaky(instance: Any) -> dict:
        calls.append(instance)
        raise TimeoutError("nope")

    with pytest.raises(TimeoutError):
        wrap_with_transient_retry(always_flaky)({})

    assert len(calls) == MAX_TRIES


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_a_retryable_status_is_transient(status: int) -> None:
    calls: list[Any] = []

    def throttled(instance: Any) -> dict:
        calls.append(instance)
        if len(calls) < 2:
            exc = RuntimeError("rate limited")
            exc.status_code = status  # type: ignore[attr-defined]
            raise exc
        return {"score": 1.0}

    assert wrap_with_transient_retry(throttled)({}) == {"score": 1.0}
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_client_error_is_not_transient(status: int) -> None:
    calls: list[Any] = []

    def refused(instance: Any) -> dict:
        calls.append(instance)
        exc = RuntimeError("denied")
        exc.status_code = status  # type: ignore[attr-defined]
        raise exc

    with pytest.raises(RuntimeError):
        wrap_with_transient_retry(refused)({})

    assert len(calls) == 1


def test_a_transient_cause_behind_a_wrapper_is_found() -> None:
    """google-auth chains on `__cause__`; httpcore leaves it on `__context__`."""
    calls: list[Any] = []

    def wrapped(instance: Any) -> dict:
        calls.append(instance)
        if len(calls) < 2:
            try:
                raise ConnectionError("reset")
            except ConnectionError as exc:
                raise RuntimeError("wrapped") from exc
        return {"score": 1.0}

    assert wrap_with_transient_retry(wrapped)({}) == {"score": 1.0}
    assert len(calls) == 2


def test_a_metric_loaded_from_a_library_is_wrapped() -> None:
    """Wrapping happens at load, so every caller gets a retrying metric."""
    from ambient_quality_agent.tools.metrics.library import load_library

    metric = (
        b"EXPECTED = 'x'\n"
        b"_calls = []\n"
        b"def evaluate(instance):\n"
        b"    _calls.append(1)\n"
        b"    if len(_calls) < 2:\n"
        b"        raise ConnectionError('reset')\n"
        b"    return {'score': 1.0}\n"
    )
    config = b"metrics_to_run: [m]\ncustom_metrics:\n  - name: m\n    custom_function_file: m.py\n"

    library = load_library({"eval_config.yaml": config, "m.py": metric})

    assert library["m"].evaluate({}) == {"score": 1.0}


def test_the_backoff_curve_matches_backoff_expo(slept: list[float]) -> None:
    """`backoff.expo` with `max_value=8` gives 2, 4, 8, 8 -- full jitter then
    picks within each. A curve an order of magnitude shorter would retry a
    rate-limited endpoint before it has recovered."""

    def always_flaky(instance: Any) -> dict:
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError):
        wrap_with_transient_retry(always_flaky)({})

    assert slept == [2.0, 4.0, 8.0, 8.0], "ceilings between the five attempts"


def test_the_wait_is_capped(slept: list[float]) -> None:
    def always_flaky(instance: Any) -> dict:
        raise TimeoutError("nope")

    with pytest.raises(TimeoutError):
        wrap_with_transient_retry(always_flaky)({})

    assert max(slept) == retry.MAX_WAIT_SECONDS


def test_a_slow_metric_stops_at_the_elapsed_budget(
    monkeypatch: pytest.MonkeyPatch, slept: list[float]
) -> None:
    """Five attempts is the usual limit; the budget catches a metric slow enough
    that retrying it would sit on the sweep for minutes."""
    clock = iter([0.0, retry.MAX_ELAPSED_SECONDS + 1.0])
    monkeypatch.setattr(retry.time, "monotonic", lambda: next(clock))
    calls: list[Any] = []

    def slow(instance: Any) -> dict:
        calls.append(instance)
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError):
        wrap_with_transient_retry(slow)({})

    assert len(calls) == 1, "the budget stopped it well before MAX_TRIES"
    assert slept == []


def test_a_successful_metric_never_sleeps(slept: list[float]) -> None:
    assert wrap_with_transient_retry(lambda i: {"score": 1.0})({}) == {
        "score": 1.0
    }
    assert slept == []


def test_each_attempt_gets_a_clean_instance(slept: list[float]) -> None:
    """A metric that mutates what it is handed must not poison its own retry,
    or the failure the operator reads names the wrong cause."""
    seen: list[dict] = []

    def mutates_then_fails(instance: dict) -> dict:
        seen.append(copy.deepcopy(instance))
        instance["turns"].append("junk")
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError):
        wrap_with_transient_retry(mutates_then_fails)({"turns": []})

    assert seen == [{"turns": []}] * retry.MAX_TRIES


def test_the_caller_s_instance_is_never_mutated() -> None:
    instance: dict[str, list[str]] = {"turns": []}
    wrap_with_transient_retry(lambda i: i["turns"].append("junk") or 1.0)(
        instance
    )
    assert instance == {"turns": []}
