# Copyright 2025 Google LLC
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

"""Client-contract DTOs for playout attribution (Phase 10).

AdPlayRecord mirrors the client's CMS end-of-day batch record (Email 5,
project_context.md): which ad played on which screen, when, advertising
which products. BlueZooVisitInterval mirrors ONE row of BlueZoo's
sensor_visits — and nothing else (replan amendment: scoped to sensor_visits
fields only — the six occupancy fields below are acknowledged sensor_visitors
drift, kept optional and demo-only rather than removed; extra="forbid" still
rejects anything new). In demo mode both shapes are produced deterministically
(app/demo_data/attribution.py, seed.py); Phase 11 connected mode fills the
same shapes from the real CMS/BlueZoo feeds.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AdPlayRecord(BaseModel):
    """One playout of one creative on one screen for one 15-min slot."""

    screen_id: int
    ad_campaign_id: int
    video_id: int
    ad_name: str | None = None
    product_ids: list[int] = Field(default_factory=list)
    start: datetime
    end: datetime


class BlueZooVisitInterval(BaseModel):
    """One sensor_visits row: visit counts at one screen for one 15-min slot."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    screen_id: int
    ad_campaign_id: int | None = None
    incoming_inner_count: float
    outgoing_inner_count: float
    incoming_outer_count: float
    outgoing_outer_count: float
    # Occupancy fields (BlueZoo `sensor_visitors` columns, NOT `sensor_visits`)
    # — DEMO-POPULATED ONLY. The live conformer (Phase 11b) queries
    # sensor_visits alone and leaves these None: populating them would cost a
    # second table scan against BlueZoo's metered bytes-scanned allowance, for
    # values nothing in this app reads (the attribution join consumes only
    # incoming_inner_count and outgoing_outer_count). If a real metric ever
    # needs occupancy, add the sensor_visitors query then — do NOT assume
    # these are live-populated. See docs/METRICS.md (circulation entry).
    minimum_visitors_inner: float | None = None
    maximum_visitors_inner: float | None = None
    average_visitors_inner: float | None = None
    minimum_visitors_outer: float | None = None
    maximum_visitors_outer: float | None = None
    average_visitors_outer: float | None = None
    valid: bool = True
