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

"""Workflow nodes for the AQA investigation pipeline.

Wiring::

    eval_multi_turn_node,              review_node
    eval_single_turn_node                  │ ReviewEvaluation, CodeMetricEvaluation,
        │ VertexEvaluation                 │ RemoteCodeMetricEvaluation
        └────────────────┬─────────────────┘
                         │ run_sweep()
                         ▼
                      _sweep ──► BaseFetcher        via _common.fetcher_factory()
                         │ Evaluation.evaluate() appends
                         ▼
                state["finding_sets"]
                         │ merge_finding_sets()
                         ▼
              insight_correlation_node ──► InsightWriter
                                           via _common.insight_store_factory()

    init_node ── delete_run_trajectories() ──► TrajectoryRecorder
                                               via _common.trajectory_recorder_factory()

Each node lives in its own submodule; the node instances are imported
by ``workflow.py``.
"""

from ambient_quality_agent.core.nodes.eval import (
    eval_multi_turn_node,
    eval_single_turn_node,
)
from ambient_quality_agent.core.nodes.init import init_node
from ambient_quality_agent.core.nodes.insight_correlation import (
    insight_correlation_node,
)
from ambient_quality_agent.core.nodes.review import review_node

__all__ = [
    "eval_multi_turn_node",
    "eval_single_turn_node",
    "init_node",
    "insight_correlation_node",
    "review_node",
]
