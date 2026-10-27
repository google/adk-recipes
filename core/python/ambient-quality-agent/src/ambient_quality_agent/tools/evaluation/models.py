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

"""Shared evaluation data models."""

from __future__ import annotations

import enum

from agentplatform._genai.types import Metric, PrebuiltMetric


class MetricType(enum.StrEnum):
    """Evaluation scope selecting the query structure and mapping format.

    ``MULTI_TURN`` evaluates a whole session trajectory; ``SINGLE_TURN``
    evaluates one invocation/turn in isolation.
    """

    MULTI_TURN = "MULTI_TURN"
    SINGLE_TURN = "SINGLE_TURN"


MULTI_TURN_METRICS: dict[str, Metric] = {
    "task_success": PrebuiltMetric.MULTI_TURN_TASK_SUCCESS,
    "tool_use_quality": PrebuiltMetric.MULTI_TURN_TOOL_USE_QUALITY,
    "trajectory_quality": PrebuiltMetric.MULTI_TURN_TRAJECTORY_QUALITY,
}
"""Prebuilt metrics evaluated across complete multi-turn conversations."""

SINGLE_TURN_METRICS: dict[str, Metric] = {
    "final_response_quality": PrebuiltMetric.FINAL_RESPONSE_QUALITY,
    "hallucination": PrebuiltMetric.HALLUCINATION,
    "tool_use_quality": PrebuiltMetric.TOOL_USE_QUALITY,
    "safety": PrebuiltMetric.SAFETY,
}
"""Prebuilt metrics evaluated across isolated single turns."""
