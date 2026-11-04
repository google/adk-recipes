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

"""Session reviewer that inspects one session per run using an ADK agent and tools.

The initial message embeds the elided trajectory JSON (`elided_trajectory`).
The `query_trajectory` jq tool (`tools.jq`) queries the full trajectory
(`trajectory_json`). Gemini models run at `agent.THINKING_LEVEL` and Claude models
(`agent.CLAUDE_MODEL_PREFIX`) run with adaptive thinking at `agent.CLAUDE_EFFORT`
and prompt caching, both returning thought summaries. The instruction asks for
``{"findings": [...], "dismissed": [...]}`` when the argon rules apply
(`prompts.ARGON_RULES`, enabled by default for Gemini only), or for
``{"findings": [...]}`` otherwise. `final_answer` reads only ``findings``
from the first reply without a function call.

`extract_findings` returns only the findings of one session review; bind it to
a shared client to review many sessions with one client.

Wiring::

    extract_findings()
        ▼
    reviewer.review_session()
        │ prompts.build_review_prompt()
        │   ──► trajectory_json.build_trajectory_json()
        │   ──► prompts.resolve_system_instruction()
        │   ──► prompts.render_goal_section()
        │   ──► elided_trajectory.render_elided_trajectory()
        │ tools.jq.JqQueryTool()
        │ agent.ReviewLoopController()
        │ agent.build_reviewer_agent()
        │   ──► agent.create_client() (Gemini, no client injected)
        │   ──► LlmAgent(Gemini or agent._ReviewClaude, JqQueryTool,
        │         agent.ReviewLoopController callbacks)
        ▼
    reviewer._run_review()
        ──► reviewer._request_final_reply() ──► Runner.run_async()
        ├─► ReviewLoopController.before_model_callback()
        │     system instruction, pruning, forced final answer
        ├─► Gemini ──► client.aio.models.generate_content()
        │   or _ReviewClaude ──► AsyncAnthropicVertex.messages.create()
        ├─► ReviewLoopController.before_tool_callback()  undeclared tool names
        └─► JqQueryTool.run_async() (calls of one reply run concurrently)
              ──► JqQueryTool.query_trajectory() (worker thread)
                    ──► jq.run_jq() ──► _jq_worker.py (child process)
                    ──► jq.render_truncation_note()
        │ final reply ──► final_answer.extract_reply_text()
        ▼
    final_answer.parse_final_text()
        ──► final_answer.extract_raw_findings()
        ──► final_answer.build_findings()
        (no findings JSON: one prompts.TEXT_REASK on the same session)
        │ outcome or error
        ▼
    reviewer._log_review_summary()
"""

from ambient_quality_agent.tools.review.agentic_reviewer.agent import (
    create_client,
)
from ambient_quality_agent.tools.review.agentic_reviewer.prompts import (
    ReviewPrompt,
    build_review_prompt,
    render_goal_section,
)
from ambient_quality_agent.tools.review.agentic_reviewer.reviewer import (
    UnparsableReplyError,
    extract_findings,
    review_session,
)

__all__ = [
    "ReviewPrompt",
    "UnparsableReplyError",
    "build_review_prompt",
    "create_client",
    "extract_findings",
    "render_goal_section",
    "review_session",
]
