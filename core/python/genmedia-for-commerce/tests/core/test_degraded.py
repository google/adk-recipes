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

"""Tests for ``core.degraded``: fallbacks reported across threads and
tasks."""

import asyncio

from genmedia4commerce.core import degraded
from genmedia4commerce.core.retry import ContextPool


def test_a_report_outside_tracking_does_nothing():
    degraded.report("nobody listens")
    with degraded.tracking() as marker:
        pass
    assert marker.ok and marker.reasons == []


def test_reports_reach_the_marker_from_threads_pools_and_tasks():
    async def run() -> degraded.Marker:
        with degraded.tracking() as marker:
            await asyncio.to_thread(degraded.report, "to_thread")
            with ContextPool(max_workers=2) as pool:
                list(pool.map(degraded.report, ["pool-1", "pool-2"]))

            async def later():
                await asyncio.to_thread(degraded.report, "task thread")

            task = asyncio.create_task(later())
        await task  # created inside the block: it keeps the marker
        return marker

    marker = asyncio.run(run())
    assert not marker.ok
    assert sorted(marker.reasons) == sorted(
        ["to_thread", "pool-1", "pool-2", "task thread"]
    )


def test_a_nested_marker_keeps_its_own_reports():
    async def run():
        with degraded.tracking() as outer:

            async def branch():
                with degraded.tracking() as inner:
                    await asyncio.to_thread(degraded.report, "inner")
                return inner

            inner, _ = await asyncio.gather(
                branch(), asyncio.to_thread(degraded.report, "outer")
            )
        return outer, inner

    outer, inner = asyncio.run(run())
    assert (outer.reasons, inner.reasons) == (["outer"], ["inner"])


def test_the_same_marker_can_track_several_blocks():
    marker = degraded.Marker()
    with degraded.tracking(marker):
        degraded.report("first")
    degraded.report("outside")
    with degraded.tracking(marker):
        degraded.report("second")
    assert marker.reasons == ["first", "second"]
