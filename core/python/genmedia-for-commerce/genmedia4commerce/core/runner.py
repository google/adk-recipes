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

"""The fan-out runner: N variations (parallel) or N attempts (sequential).

Generation and evaluation are supplied by the workflow; the runner streams a
``VariationEvent`` per finished variation and ends with one ``CompleteEvent``.
Each variation gets ``RunPolicy.deadline_s`` for make and evaluate together.
When it times out or is cancelled, its model calls still running in threads
stop retrying (``core.retry.CANCEL``); a call already in flight finishes.
"""

import asyncio
import contextlib
import dataclasses
import logging
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

from genmedia4commerce.core import trace
from genmedia4commerce.core.errors import user_message
from genmedia4commerce.core.events import CompleteEvent, Event, VariationEvent
from genmedia4commerce.core.retry import CANCEL
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Verdict

__all__ = [
    "Evaluate",
    "Make",
    "RankKey",
    "RunPolicy",
    "Variation",
    "default_rank_key",
    "run_variations",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")
Status = Literal["ready", "rejected", "error"]


@dataclass(frozen=True)
class RunPolicy:
    """How many outputs to make and how long each may take.

    ``deadline_s`` covers one variation: make and evaluate together.
    """

    mode: Literal["parallel", "sequential"] = "parallel"
    num_variations: int = 3
    max_attempts: int = 5
    deadline_s: float = 900

    @classmethod
    def from_settings(cls) -> "RunPolicy":
        """Build the deployment default from ``core.settings``."""
        s = get_settings()
        return cls(
            mode=s.run_mode,
            num_variations=s.num_variations,
            max_attempts=s.max_attempts,
        )

    def replace(self, **kw) -> "RunPolicy":
        """Return a copy with the given fields changed."""
        return dataclasses.replace(self, **kw)


@dataclass
class Variation(Generic[T]):
    """One finished variation (or attempt)."""

    index: int
    status: Status
    output: T | None
    verdict: Verdict | None
    score: float
    attempt: int
    error: str | None = None
    # Seconds spent making (generate_s) and judging (check_s) it, each set
    # once its step ends (a check that timed out has no check_s).
    timing: dict = field(default_factory=dict)


# Make: arg is the variation index (parallel) or attempt number (sequential).
Make = Callable[[int], Awaitable[T]]
# Evaluate: returns (verdict, possibly-corrected output).
Evaluate = Callable[[T], Awaitable[tuple[Verdict, T]]]
RankKey = Callable[[Variation[T]], tuple]


def default_rank_key(v: Variation | VariationEvent) -> tuple:
    """Ready first, then published, fewer imperfections, higher score (a
    variation or its event)."""
    publish = bool(v.verdict and v.verdict.publish)
    imperfections = v.verdict.imperfection_count if v.verdict else 0
    return (v.status != "ready", not publish, imperfections, -v.score)


def _default_score(verdict: Verdict) -> float:
    return float(verdict.details.get("score", 0.0))


async def _make_and_evaluate(
    make: Make[T], evaluate: Evaluate[T], arg: int, timing: dict
) -> tuple[Verdict, T]:
    """Make, then judge; each step's seconds go into ``timing``."""
    start = time.monotonic()
    output = await make(arg)
    timing["generate_s"] = round(time.monotonic() - start, 1)
    start = time.monotonic()
    result = await evaluate(output)
    timing["check_s"] = round(time.monotonic() - start, 1)
    return result


async def _run_one(
    policy: RunPolicy,
    make: Make[T],
    evaluate: Evaluate[T],
    score: Callable[[Verdict], float],
    *,
    index: int,
    attempt: int,
    arg: int,
) -> Variation[T]:
    """One variation, traced with its index and attempt (``core.trace``)."""
    with trace.scope(variation=index, attempt=attempt):
        v = await _variation(
            policy, make, evaluate, score, index=index, attempt=attempt, arg=arg
        )
        result = {
            "status": v.status,
            "score": v.score,
            "error": v.error,
            "timing": v.timing,
        }
        trace.save("verdict", {**result, "verdict": v.verdict})
    return v


async def _variation(
    policy: RunPolicy,
    make: Make[T],
    evaluate: Evaluate[T],
    score: Callable[[Verdict], float],
    *,
    index: int,
    attempt: int,
    arg: int,
) -> Variation[T]:
    cancel = threading.Event()
    token = CANCEL.set(cancel)
    timing: dict = {}
    try:
        verdict, output = await asyncio.wait_for(
            _make_and_evaluate(make, evaluate, arg, timing),
            timeout=policy.deadline_s,
        )
    except TimeoutError:
        message = f"DeadlineExceeded: timed out after {policy.deadline_s:g}s"
        return Variation(
            index, "error", None, None, 0.0, attempt, message, timing
        )
    except Exception as exc:
        logger.warning(f"variation {index} attempt {attempt} failed: {exc}")
        return Variation(
            index, "error", None, None, 0.0, attempt, user_message(exc), timing
        )
    finally:
        cancel.set()  # stops this variation's calls still running in threads
        CANCEL.reset(token)
    if verdict.error:
        status: Status = "error"
    else:
        status = "ready" if verdict.publish else "rejected"
    error = verdict.description if verdict.error else None
    return Variation(
        index, status, output, verdict, score(verdict), attempt, error, timing
    )


async def _parallel(
    policy, make, evaluate, score
) -> AsyncGenerator[Variation, None]:
    tasks = [
        asyncio.create_task(
            _run_one(policy, make, evaluate, score, index=i, attempt=1, arg=i)
        )
        for i in range(policy.num_variations)
    ]
    try:
        for next_done in asyncio.as_completed(tasks):
            yield await next_done
    finally:
        pending = [t for t in tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _sequential(
    policy, make, evaluate, score
) -> AsyncGenerator[Variation, None]:
    for attempt in range(1, policy.max_attempts + 1):
        v = await _run_one(
            policy,
            make,
            evaluate,
            score,
            index=attempt - 1,
            attempt=attempt,
            arg=attempt,
        )
        yield v
        if v.status == "ready":
            return


def _event(v: Variation) -> VariationEvent:
    return VariationEvent(
        index=v.index,
        status=v.status,
        media=v.output if isinstance(v.output, bytes) else None,
        verdict=v.verdict,
        score=v.score,
        attempt=v.attempt,
        error=v.error,
        timing=v.timing,
        fixed=bool(v.verdict and v.verdict.details.get("fixed")),
    )


async def run_variations(
    policy: RunPolicy,
    make: Make[T],
    evaluate: Evaluate[T],
    *,
    rank_key: RankKey = default_rank_key,
    score: Callable[[Verdict], float] | None = None,
) -> AsyncGenerator[Event, None]:
    """Run ``make`` + ``evaluate`` per policy and stream the events.

    Exceptions from ``make``/``evaluate`` become ``status="error"``
    variations; cancelling the consumer cancels running tasks.
    """
    run = _parallel if policy.mode == "parallel" else _sequential
    finished: list[Variation] = []
    async with contextlib.aclosing(
        run(policy, make, evaluate, score or _default_score)
    ) as variations:
        async for v in variations:
            finished.append(v)
            yield _event(v)
    ranked = sorted(finished, key=rank_key)
    yield CompleteEvent(
        ranked_indices=[v.index for v in ranked],
        best_index=next((v.index for v in ranked if v.status == "ready"), None),
    )
