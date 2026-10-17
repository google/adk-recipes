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
"""Selection: keeps the best verified candidates as parents for the next round.

Pure functions over plain dicts so the population can live in ADK session
state and be unit tested without a model.
"""

from typing import Any


def rank_key(candidate: dict[str, Any]) -> tuple[float, int]:
    """Orders candidates best first: lower score, then earlier id."""
    return (candidate["score"], candidate["id"])


def add_candidate(
    population: list[dict[str, Any]],
    candidate: dict[str, Any],
    max_size: int,
) -> list[dict[str, Any]]:
    """Returns a new population with `candidate` added if it earns a place.

    Only valid candidates are kept. Ties on score keep the earlier candidate,
    so the population does not churn on equivalent programs. Lower scores are
    better.
    """
    if not candidate.get("valid"):
        return list(population)
    merged = [*population, candidate]
    merged.sort(key=rank_key)
    return merged[:max_size]


def best(population: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Returns the best candidate, or None if the population is empty."""
    return min(population, key=rank_key, default=None)


def summarize(population: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Returns a compact view of the population (without code) for the model."""
    return [
        {"id": c["id"], "score": c["score"], "idea": c.get("idea", "")}
        for c in sorted(population, key=rank_key)
    ]
