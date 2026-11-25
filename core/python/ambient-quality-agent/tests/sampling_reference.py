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


"""Python reference of the weighted sampling rule in
`ambient_quality_agent.tools.ingestion.sampling`, for tests.

Investigations run the SQL; this restates it so tests can check the rule's
properties on fixed inputs.
"""

from __future__ import annotations

import bisect
import dataclasses
import random
from collections.abc import Sequence

from ambient_quality_agent.tools.ingestion.sampling import (
    ABNORMAL_FINISH_COEFFICIENT,
    ADDED_WEIGHT_AVERAGE_LIMIT,
    BUSY_TURN_COEFFICIENT,
    COST_COEFFICIENT,
    ERROR_COEFFICIENT,
    LATENCY_COEFFICIENT,
    TAIL_CUTOFF,
    TOOL_REPORTED_ERROR_COEFFICIENT,
)
from ambient_quality_agent.tools.trajectories.models import SamplingSignals


@dataclasses.dataclass(frozen=True)
class TrajectoryMeasures:
    """One candidate's measures, as a source's measures SQL projects them."""

    has_error: bool = False
    has_abnormal_finish: bool = False
    has_tool_reported_error: bool = False
    busy_turn_calls: int | None = None
    total_tokens: int | None = None
    slowest_turn_ms: int | None = None


def compute_percentiles(values: Sequence[float | None]) -> list[float | None]:
    """Computes the percentile of each value among the recorded ones, as the SQL does.

    Args:
        values: One measure per candidate, None where it is not recorded.

    Returns:
        One percentile from 0 to 1 per value, in order, and None where the
        value is None.
    """
    present = sorted(value for value in values if value is not None)
    # PERCENT_RANK() of a single row is 0.
    denominator = max(1, len(present) - 1)
    return [
        None
        if value is None
        else bisect.bisect_left(present, value) / denominator
        for value in values
    ]


def compute_tail_score(percentile: float | None) -> float:
    """Computes the tail score of a percentile, as the SQL does.

    Args:
        percentile: A percentile from `compute_percentiles`.

    Returns:
        A score from 0 to 1, and 0 for None.
    """
    if percentile is None:
        return 0.0
    return max(0.0, (percentile - TAIL_CUTOFF) / (1 - TAIL_CUTOFF))


def compute_weights(
    measures: Sequence[TrajectoryMeasures],
) -> list[tuple[float, SamplingSignals]]:
    """Computes each candidate's weight and signals, as the SQL does.

    Args:
        measures: Every candidate's measures.

    Returns:
        One ``(weight, signals)`` pair per candidate, in order.
    """
    busy = compute_percentiles([m.busy_turn_calls for m in measures])
    cost = compute_percentiles([m.total_tokens for m in measures])
    latency = compute_percentiles([m.slowest_turn_ms for m in measures])
    signals = [
        SamplingSignals(
            has_error=m.has_error,
            has_abnormal_finish=m.has_abnormal_finish,
            has_tool_reported_error=m.has_tool_reported_error,
            busy_turn_calls=m.busy_turn_calls,
            total_tokens=m.total_tokens,
            slowest_turn_ms=m.slowest_turn_ms,
            busy_turn_percentile=busy[index],
            cost_percentile=cost[index],
            latency_percentile=latency[index],
            busy_turn_tail=compute_tail_score(busy[index]),
            cost_tail=compute_tail_score(cost[index]),
            latency_tail=compute_tail_score(latency[index]),
        )
        for index, m in enumerate(measures)
    ]
    added = [
        ERROR_COEFFICIENT * s.has_error
        + ABNORMAL_FINISH_COEFFICIENT * s.has_abnormal_finish
        + TOOL_REPORTED_ERROR_COEFFICIENT * s.has_tool_reported_error
        + BUSY_TURN_COEFFICIENT * s.busy_turn_tail
        + COST_COEFFICIENT * s.cost_tail
        + LATENCY_COEFFICIENT * s.latency_tail
        for s in signals
    ]
    average = sum(added) / len(added) if added else 0.0
    scale = max(ADDED_WEIGHT_AVERAGE_LIMIT, average)
    return [
        (1 + value / scale, signal)
        for value, signal in zip(added, signals, strict=True)
    ]


def draw_priority_sample(
    weights: Sequence[float], limit: int, rng: random.Random
) -> list[tuple[int, float]]:
    """Draws a priority sample, as the SQL does with random keys from `rng`.

    Args:
        weights: Every candidate's weight, each at least 1.
        limit: The budget.
        rng: Source of the random keys.

    Returns:
        ``(index, inclusion_probability)`` for each picked candidate, highest
        priority first.
    """
    # 1 - random() lies in (0, 1], the range of the SQL key.
    priorities = sorted(
        (
            (weight / (1 - rng.random()), index)
            for index, weight in enumerate(weights)
        ),
        reverse=True,
    )
    kept = priorities[:limit]
    if len(priorities) <= limit:
        return [(index, 1.0) for _, index in kept]
    threshold = priorities[limit][0]
    return [(index, min(1.0, weights[index] / threshold)) for _, index in kept]
