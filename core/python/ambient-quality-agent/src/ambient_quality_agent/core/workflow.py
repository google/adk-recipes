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

"""ADK workflow definition for the Ambient Quality Agent.

Wiring::

    run_investigation_graph(), harness.run_async(), local_run.main()
        │ Runner.run_async()
        ▼
    investigation_workflow (Workflow, state: WorkflowState)
        │
        ▼
    START ──► init_node
                │ route = quality_analysis_mode
                ├── "session_review" ──► review_node ─────────────┐
                │                                                 ▼
                └── "eval_service" ──► eval_multi_turn_node   insight_correlation_node
                                           │                      ▲
                                           ▼                      │
                                       eval_single_turn_node ─────┘
"""

from google.adk.workflow import START, Workflow

from .nodes import (
    eval_multi_turn_node,
    eval_single_turn_node,
    init_node,
    insight_correlation_node,
    review_node,
)
from .state import WorkflowState

investigation_workflow = Workflow(
    name="aqa_investigation_workflow",
    description=("On-demand agent investigation pipeline."),
    state_schema=WorkflowState,
    edges=[
        # Route `init` to the selected producer branch via `quality_analysis_mode`.
        # `insight_correlation` executes once either upstream branch completes.
        (
            START,
            init_node,
            {
                "session_review": review_node,
                "eval_service": eval_multi_turn_node,
            },
        ),
        (
            eval_multi_turn_node,
            eval_single_turn_node,
            insight_correlation_node,
        ),
        (review_node, insight_correlation_node),
    ],
)
