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

"""Wire names shared by the agent and its front-ends.

The single definition of the HTTP command route paths: the CLI, the dashboard,
the agent's own route module, and the local mock server all read them. The
agent can read this package because the container image ships it.

It also holds the chat agent's A2A app name, which the CLI and the dashboard
read. `tools/test_mock_aqa.py` pins it against the chat agent's own name.
"""

from __future__ import annotations

# Command route paths, the argument `AgentClient.post_command` takes. The agent
# mounts them, the front-ends call them, and the mock server stands in for the
# agent locally. All three read them from here.

INVESTIGATIONS_LIST_ROUTE = "investigations/list"
INVESTIGATIONS_GET_ROUTE = "investigations/get"
INVESTIGATIONS_STATS_ROUTE = "investigations/stats"
INVESTIGATIONS_DAILY_ROUTE = "investigations/daily"
INVESTIGATIONS_SCHEDULE_ROUTE = "investigations/schedule"
INSIGHTS_LIST_ROUTE = "insights/list"
INSIGHTS_GET_ROUTE = "insights/get"
INSIGHTS_DISMISS_ROUTE = "insights/dismiss"
INSIGHTS_MERGE_ROUTE = "insights/merge"
TRAJECTORIES_OUTCOMES_ROUTE = "trajectories/outcomes"
TRAJECTORIES_LIST_ROUTE = "trajectories/list"
TRAJECTORIES_CASE_ROUTE = "trajectories/case"
GOAL_GET_ROUTE = "documents/goal/get"
GOAL_SET_ROUTE = "documents/goal/set"
GOAL_VERSIONS_ROUTE = "documents/goal/versions"
MEMORIES_LIST_ROUTE = "documents/memories/list"
MEMORIES_DELETE_ROUTE = "documents/memories/delete"
HEALTH_ROUTE = "health"
SOURCE_ROUTE = "source"
SOURCE_UPLOAD_ROUTE = "source/upload"
SOURCE_COMMIT_ROUTE = "source/commit"
CONFIG_ROUTE = "config"
AGENTS_LIST_ROUTE = "agents/list"
AGENTS_ATTACH_ROUTE = "agents/attach"
AGENTS_DETACH_ROUTE = "agents/detach"
AGENTS_APPLIED_ROUTE = "agents/applied"

# The chat agent's A2A app name, the path segment under `/a2a/`. The CLI's `run`
# and the dashboard's chat both reach the chat agent through it.
CHAT_A2A_APP = "aqa_chat"
