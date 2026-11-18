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

"""A Photo Try-On job's results after the judge's ranking: the fronts in
its order, the back views (drawn from the best front) right after it."""

import pytest

from genmedia4commerce.core.events import (
    CompleteEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.studio import jobs


def _variation(index: int, score: float, view: str, **extra):
    event = VariationEvent(
        index=index, status="ready", media=None, verdict=None, score=score
    )
    event.extra = {"view": view, **extra}
    event.media_uri = f"gs://b/runs/x/{view}-{index}.png"
    return event


@pytest.fixture(autouse=True)
def _no_signing(monkeypatch):
    monkeypatch.setattr(jobs.sm, "preview_url", lambda uri, video=False: uri)


def _shown(job: dict) -> list[tuple[str, int]]:
    return [(r["view"], r["index"]) for r in job["results"]]


async def _photo_try_on(fronts, backs, ranked, best) -> dict:
    """Fold ``run_tryon``'s events into a job: the fronts, then the back
    views (the back ranking rides on the last, as ``back_view`` sends it),
    then the front ranking."""
    job: dict = {"results": [], "image_results": []}
    events = [_variation(i, score, "front") for i, score in fronts]
    if backs:
        events.append(ProgressEvent("back", "Generating the back view"))
        events += [_variation(i, score, "back") for i, score in backs]
        events[-1].extra.update(ranked=[b[0] for b in backs], best_index=0)
    events.append(CompleteEvent(ranked_indices=ranked, best_index=best))
    for event in events:
        await jobs._apply(job, event, video=False)
    return job


@pytest.mark.asyncio
async def test_back_views_follow_the_best_front_after_the_ranking():
    job = await _photo_try_on(
        fronts=[(0, 70.0), (2, 80.0), (1, 95.0)],  # in finishing order
        backs=[(0, 88.0), (1, 91.0)],
        ranked=[1, 2, 0],
        best=1,
    )
    assert _shown(job) == [
        ("front", 1),  # "Best": the first result
        ("back", 0),
        ("back", 1),
        ("front", 2),
        ("front", 0),
    ]


@pytest.mark.parametrize("best", [0, 1, 2])
@pytest.mark.asyncio
async def test_the_first_result_is_always_the_best_front(best):
    others = [i for i in range(3) if i != best]
    job = await _photo_try_on(
        fronts=[(i, 60.0 + 10 * (i == best)) for i in range(3)],
        backs=[(i, 90.0) for i in range(3)],
        ranked=[best, *others],
        best=best,
    )
    assert _shown(job)[0] == ("front", best)
    assert _shown(job)[1:4] == [("back", 0), ("back", 1), ("back", 2)]
    assert _shown(job)[4:] == [("front", i) for i in others]


@pytest.mark.asyncio
async def test_without_back_views_the_fronts_are_in_the_judges_order():
    job = await _photo_try_on(
        fronts=[(0, 70.0), (1, 95.0), (2, 80.0)],
        backs=[],
        ranked=[1, 2, 0],
        best=1,
    )
    assert _shown(job) == [("front", 1), ("front", 2), ("front", 0)]
