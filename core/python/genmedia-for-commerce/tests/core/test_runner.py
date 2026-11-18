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

import asyncio

import pytest

from genmedia4commerce.core.events import CompleteEvent, VariationEvent
from genmedia4commerce.core.runner import (
    RunPolicy,
    Variation,
    default_rank_key,
    run_variations,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Verdict


async def collect(agen):
    return [ev async for ev in agen]


def split(events):
    variations = [e for e in events if isinstance(e, VariationEvent)]
    completes = [e for e in events if isinstance(e, CompleteEvent)]
    assert len(completes) == 1 and events[-1] is completes[0]
    return variations, completes[0]


def verdict(publish, imperfections=0, score=0.0, error=False):
    return Verdict(
        publish=publish,
        imperfection_count=imperfections,
        error=error,
        details={"score": score},
    )


def make_bytes(prefix=b"out"):
    async def make(i):
        return prefix + str(i).encode()

    return make


@pytest.mark.asyncio
async def test_parallel_all_publish():
    scores = {b"out0": 10.0, b"out1": 30.0, b"out2": 20.0}

    async def evaluate(out):
        return verdict(True, score=scores[out]), out

    events = await collect(
        run_variations(RunPolicy(num_variations=3), make_bytes(), evaluate)
    )
    variations, complete = split(events)
    assert len(variations) == 3
    assert {v.status for v in variations} == {"ready"}
    assert {v.attempt for v in variations} == {1}
    assert complete.ranked_indices == [1, 2, 0]
    assert complete.best_index == complete.ranked_indices[0] == 1
    by_index = {v.index: v for v in variations}
    assert by_index[1].media == b"out1"
    assert by_index[1].score == 30.0


@pytest.mark.asyncio
async def test_a_fixed_output_is_marked_on_its_event():
    """``details["fixed"]`` (the try-on's fix pass) becomes the event's
    top-level ``fixed``; false otherwise."""

    async def evaluate(out):
        fixed = out == b"out0"
        return Verdict(publish=True, details={"fixed": fixed}), out

    policy = RunPolicy(num_variations=2)
    variations, _ = split(
        await collect(run_variations(policy, make_bytes(), evaluate))
    )
    by_index = {v.index: v for v in variations}
    assert (by_index[0].fixed, by_index[1].fixed) == (True, False)
    assert by_index[0].to_dict()["fixed"] is True
    assert by_index[1].to_dict()["fixed"] is False


@pytest.mark.asyncio
async def test_parallel_one_make_raises():
    async def make(i):
        if i == 1:
            raise RuntimeError("boom")
        return b"x%d" % i

    async def evaluate(out):
        return verdict(True), out

    events = await collect(run_variations(RunPolicy(), make, evaluate))
    variations, complete = split(events)
    by_index = {v.index: v for v in variations}
    assert by_index[1].status == "error"
    # An unexpected error's text stays in the logs; clients get the generic message.
    assert by_index[1].error == "the generation failed on the server; try again"
    assert by_index[1].media is None
    assert by_index[0].status == by_index[2].status == "ready"
    assert complete.best_index in (0, 2)
    assert complete.ranked_indices[-1] == 1


@pytest.mark.asyncio
async def test_parallel_all_rejected_ranked_by_imperfections():
    imperfections = {b"out0": 5, b"out1": 1, b"out2": 3}

    async def evaluate(out):
        return verdict(False, imperfections[out]), out

    events = await collect(run_variations(RunPolicy(), make_bytes(), evaluate))
    variations, complete = split(events)
    assert {v.status for v in variations} == {"rejected"}
    assert complete.best_index is None
    assert complete.ranked_indices == [1, 2, 0]


@pytest.mark.asyncio
async def test_verdict_error_is_error_status():
    async def evaluate(out):
        return Verdict(
            publish=False, error=True, description="eval incomplete"
        ), out

    events = await collect(
        run_variations(RunPolicy(num_variations=1), make_bytes(), evaluate)
    )
    variations, complete = split(events)
    assert variations[0].status == "error"
    assert variations[0].error == "eval incomplete"
    assert complete.best_index is None


@pytest.mark.asyncio
async def test_sequential_stops_at_first_ready():
    seen = []

    async def make(attempt):
        seen.append(attempt)
        return b"a%d" % attempt

    async def evaluate(out):
        return verdict(out == b"a3"), out

    policy = RunPolicy(mode="sequential", max_attempts=5)
    events = await collect(run_variations(policy, make, evaluate))
    variations, complete = split(events)
    assert seen == [1, 2, 3]
    assert [v.attempt for v in variations] == [1, 2, 3]
    assert [v.index for v in variations] == [0, 1, 2]
    assert [v.status for v in variations] == ["rejected", "rejected", "ready"]
    assert complete.best_index == 2


@pytest.mark.asyncio
async def test_sequential_all_rejected():
    async def evaluate(out):
        return verdict(False), out

    policy = RunPolicy(mode="sequential", max_attempts=5)
    events = await collect(run_variations(policy, make_bytes(), evaluate))
    variations, complete = split(events)
    assert len(variations) == 5
    assert [v.attempt for v in variations] == [1, 2, 3, 4, 5]
    assert complete.best_index is None
    assert sorted(complete.ranked_indices) == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_sequential_error_continues():
    async def make(attempt):
        if attempt == 1:
            raise RuntimeError("transient")
        return b"ok"

    async def evaluate(out):
        return verdict(True), out

    policy = RunPolicy(mode="sequential", max_attempts=3)
    events = await collect(run_variations(policy, make, evaluate))
    variations, complete = split(events)
    assert [v.status for v in variations] == ["error", "ready"]
    assert complete.best_index == 1


@pytest.mark.asyncio
async def test_evaluate_corrected_output_is_emitted():
    async def evaluate(out):
        return verdict(True), b"corrected-" + out

    events = await collect(
        run_variations(RunPolicy(num_variations=1), make_bytes(), evaluate)
    )
    variations, _ = split(events)
    assert variations[0].media == b"corrected-out0"


@pytest.mark.asyncio
async def test_deadline_turns_slow_make_into_error():
    async def make(i):
        if i == 0:
            await asyncio.sleep(5)
        return b"fast"

    async def evaluate(out):
        return verdict(True), out

    policy = RunPolicy(num_variations=2, deadline_s=0.1)
    events = await collect(run_variations(policy, make, evaluate))
    variations, complete = split(events)
    slow = next(v for v in variations if v.index == 0)
    assert slow.status == "error"
    assert "DeadlineExceeded" in slow.error and "timed out" in slow.error
    assert complete.best_index == 1


@pytest.mark.asyncio
async def test_consumer_cancel_cancels_running_tasks():
    started = asyncio.Event()
    cancelled = []

    async def make(i):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(i)
            raise
        return b"never"

    async def evaluate(out):
        return verdict(True), out

    async def consume():
        async for _ in run_variations(
            RunPolicy(num_variations=3), make, evaluate
        ):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == [0, 1, 2]


@pytest.mark.asyncio
async def test_aclose_cancels_running_tasks():
    cancelled = []

    async def make(i):
        if i == 0:
            return b"first"
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(i)
            raise
        return b"never"

    async def evaluate(out):
        return verdict(True), out

    agen = run_variations(RunPolicy(num_variations=3), make, evaluate)
    first = await agen.__anext__()
    assert first.index == 0
    await agen.aclose()
    assert sorted(cancelled) == [1, 2]


@pytest.mark.asyncio
async def test_custom_score_and_rank_key():
    async def evaluate(out):
        return verdict(True, imperfections=int(out[-1:])), out

    events = await collect(
        run_variations(
            RunPolicy(),
            make_bytes(),
            evaluate,
            score=lambda v: 100.0 - v.imperfection_count,
            rank_key=lambda v: (-v.index,),
        )
    )
    variations, complete = split(events)
    assert {v.index: v.score for v in variations} == {
        0: 100.0,
        1: 99.0,
        2: 98.0,
    }
    assert complete.ranked_indices == [2, 1, 0]
    assert complete.best_index == 2


def test_default_rank_key_order():
    ready = Variation(0, "ready", b"", verdict(True, 2), 50.0, 1)
    ready_better = Variation(1, "ready", b"", verdict(True, 0), 10.0, 1)
    rejected = Variation(2, "rejected", b"", verdict(False, 0), 99.0, 1)
    error = Variation(3, "error", None, None, 0.0, 1, "x")
    ranked = sorted(
        [error, rejected, ready, ready_better], key=default_rank_key
    )
    assert [v.index for v in ranked] == [1, 0, 2, 3]


def test_run_policy_defaults_replace_and_from_settings(monkeypatch):
    p = RunPolicy()
    assert (p.mode, p.num_variations, p.max_attempts, p.deadline_s) == (
        "parallel",
        3,
        5,
        900,
    )
    q = p.replace(mode="sequential", num_variations=1)
    assert (q.mode, q.num_variations) == ("sequential", 1)
    assert p.mode == "parallel"
    monkeypatch.setenv("GENMEDIA_RUN_MODE", "sequential")
    monkeypatch.setenv("GENMEDIA_NUM_VARIATIONS", "4")
    monkeypatch.setenv("GENMEDIA_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    s = RunPolicy.from_settings()
    assert (s.mode, s.num_variations, s.max_attempts) == ("sequential", 4, 2)


@pytest.mark.asyncio
async def test_each_variation_reports_its_generate_and_check_seconds():
    async def make(i):
        await asyncio.sleep(0.05)
        return b"out"

    async def evaluate(out):
        await asyncio.sleep(0.15)
        return verdict(True), out

    events = await collect(
        run_variations(RunPolicy(num_variations=1), make, evaluate)
    )
    [v], _ = split(events)
    assert set(v.timing) == {"generate_s", "check_s"}
    assert 0.0 <= v.timing["generate_s"] < v.timing["check_s"]
    assert v.to_dict()["timing"] == v.timing  # in the API's events


@pytest.mark.asyncio
async def test_a_check_that_times_out_keeps_its_generate_seconds():
    async def evaluate(out):
        await asyncio.sleep(5)
        return verdict(True), out

    policy = RunPolicy(num_variations=1, deadline_s=0.2)
    events = await collect(run_variations(policy, make_bytes(), evaluate))
    [v], _ = split(events)
    assert v.status == "error"
    assert set(v.timing) == {"generate_s"}  # made, never judged
