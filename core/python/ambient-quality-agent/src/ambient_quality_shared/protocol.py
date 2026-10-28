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

"""Wire names and request shapes shared by the agent and its front-ends.

The single definition of the HTTP command routes: the CLI, the dashboard, the
agent's own route module, and the local mock server all read them. The agent
can read this package because the container image ships it.

Each route is a `Route`: its path, typed with the `TypedDict` its request body
must match. `post_command` on the agent clients takes the route and a body of
that type, so a type checker rejects an unknown or misspelled argument, a wrong
value type, or a missing required argument. Build the body with the
`TypedDict`'s constructor (`GetInsightArgs(insight_id=...)`): its keys are then
checked where it is built, which still holds in the tests, where
`pyproject.toml` relaxes ty's check of call arguments; and mypy cannot infer
the route's body type from a bare dict literal.

The check is static only. The agent's handlers read the body without these
types and ignore keys they do not know; `tests/test_protocol.py` pins each
`TypedDict` to the keys its handler reads. pydantic's `TypeAdapter` accepts
these classes as they are, so they can serve runtime validation as well.

It also holds the chat agent's A2A app name, which the CLI and the dashboard
read. `tools/test_mock_aqa.py` pins it against the chat agent's own name.
"""

from __future__ import annotations

from typing import Any, Generic, NotRequired, TypedDict, TypeVar

ArgsT = TypeVar("ArgsT")


class Route(str, Generic[ArgsT]):
    """A command route path, typed with the request body the route takes.

    A `str`, so that it mounts and compares as the path itself: the agent's
    route decorators, the mock server and test fakes use it as a plain string.

    Attributes:
        args: The `TypedDict` the request body must match.
    """

    args: type[ArgsT]

    def __new__(cls, path: str, args: type[ArgsT]) -> Route[ArgsT]:
        """Creates a route.

        Args:
            path: Path under the agent's `/api/` prefix.
            args: The `TypedDict` the request body must match.

        Returns:
            The route.
        """
        route = super().__new__(cls, path)
        route.args = args
        return route


class NoArgs(TypedDict):
    """The body of a route that takes no arguments."""


class AgentKeyArgs(TypedDict, total=False):
    """Selects the agent a route reads or acts on.

    Every route that reads or acts on one agent's data takes it. Omitted or
    empty, it is the default agent.
    """

    agent_key: str


class WindowArgs(AgentKeyArgs, total=False):
    """A time window; an omitted bound takes the tool's default."""

    window_start: str
    window_end: str


class GetInvestigationArgs(AgentKeyArgs):
    run_id: str


class SetGoalArgs(AgentKeyArgs):
    goal: str


class DeleteMemoryArgs(AgentKeyArgs):
    memory_id: str


class ListInsightsArgs(WindowArgs, total=False):
    status: str
    run_id: str
    # "true", "false", or "" for both.
    has_root_cause: str
    page_token: str
    # 1-based; ignored when page_token is set.
    page: int
    order_by: str
    page_size: int
    day: str


class GetInsightArgs(AgentKeyArgs):
    insight_id: str
    run_id: NotRequired[str]
    include_traces: NotRequired[bool]
    include_trajectories: NotRequired[bool]
    include_edits: NotRequired[bool]
    page_token: NotRequired[str]


class DismissInsightArgs(AgentKeyArgs):
    insight_id: str


class MergeInsightArgs(AgentKeyArgs):
    insight_id: str
    target_insight_id: str


class ListTrajectoriesArgs(WindowArgs, total=False):
    run_id: str
    outcome: str
    page_token: str


class GetCaseConversationArgs(AgentKeyArgs):
    trajectory_id: str
    run_id: NotRequired[str]


class AgentSelector(TypedDict, total=False):
    """Names an agent: by `agent_key`, else by `agent_name`.

    The source snapshot routes read neither as the default agent. The detach
    and applied handlers require one of the two, which a `TypedDict` cannot
    express.
    """

    agent_key: str
    agent_name: str


class GetSourceSnapshotArgs(AgentSelector, total=False):
    """An omitted `revision` means the newest complete one."""

    revision: str


class SourceFile(TypedDict):
    path: str
    # Gzipped, then base64-encoded.
    content: str


class UploadSourceFilesArgs(AgentSelector):
    revision: str
    files: list[SourceFile]


class CommitSourceSnapshotArgs(AgentSelector):
    revision: str
    manifest: dict[str, Any]


class MetricFile(TypedDict):
    """One file of a code-metric library, by its path in the library."""

    path: str
    # Base64-encoded.
    content: str


class PublishMetricsArgs(AgentKeyArgs):
    files: list[MetricFile]
    # Validates and plans, writing nothing.
    dry_run: NotRequired[bool]


class DestroyMetricsArgs(AgentKeyArgs, total=False):
    # Lists what would be deleted, deleting nothing. Defaults to True: only
    # `dry_run: false` deletes.
    dry_run: bool


class AttachAgentArgs(AgentSelector, total=False):
    fields: dict[str, Any]
    dry_run: bool
    # Answered only by a dry run.
    check_access: bool
    # Answered only by a dry run with `check_access`: fills in what the
    # telemetry table decides and `fields` leaves out, named in the plan's
    # `filled`. An engine that predates it ignores it.
    fill_from_table: bool


class RecordAppliedArgs(AgentSelector, total=False):
    """Carries at least one of `applied`, `granted` and `revoked`."""

    applied: dict[str, Any]
    granted: list[dict[str, str]]
    revoked: list[dict[str, str]]


# The agent mounts these, the front-ends call them, and the mock server stands
# in for the agent locally. All three read them from here.

INVESTIGATIONS_LIST_ROUTE = Route("investigations/list", WindowArgs)
INVESTIGATIONS_GET_ROUTE = Route("investigations/get", GetInvestigationArgs)
INVESTIGATIONS_STATS_ROUTE = Route("investigations/stats", WindowArgs)
INVESTIGATIONS_DAILY_ROUTE = Route("investigations/daily", WindowArgs)
INVESTIGATIONS_SCHEDULE_ROUTE = Route("investigations/schedule", AgentKeyArgs)
INSIGHTS_LIST_ROUTE = Route("insights/list", ListInsightsArgs)
INSIGHTS_GET_ROUTE = Route("insights/get", GetInsightArgs)
INSIGHTS_DISMISS_ROUTE = Route("insights/dismiss", DismissInsightArgs)
INSIGHTS_MERGE_ROUTE = Route("insights/merge", MergeInsightArgs)
TRAJECTORIES_OUTCOMES_ROUTE = Route("trajectories/outcomes", WindowArgs)
TRAJECTORIES_LIST_ROUTE = Route("trajectories/list", ListTrajectoriesArgs)
TRAJECTORIES_CASE_ROUTE = Route("trajectories/case", GetCaseConversationArgs)
GOAL_GET_ROUTE = Route("documents/goal/get", AgentKeyArgs)
GOAL_SET_ROUTE = Route("documents/goal/set", SetGoalArgs)
GOAL_VERSIONS_ROUTE = Route("documents/goal/versions", AgentKeyArgs)
MEMORIES_LIST_ROUTE = Route("documents/memories/list", AgentKeyArgs)
MEMORIES_DELETE_ROUTE = Route("documents/memories/delete", DeleteMemoryArgs)
HEALTH_ROUTE = Route("health", AgentKeyArgs)
SOURCE_ROUTE = Route("source", GetSourceSnapshotArgs)
SOURCE_UPLOAD_ROUTE = Route("source/upload", UploadSourceFilesArgs)
SOURCE_COMMIT_ROUTE = Route("source/commit", CommitSourceSnapshotArgs)
CONFIG_ROUTE = Route("config", AgentKeyArgs)
AGENTS_LIST_ROUTE = Route("agents/list", NoArgs)
AGENTS_ATTACH_ROUTE = Route("agents/attach", AttachAgentArgs)
AGENTS_DETACH_ROUTE = Route("agents/detach", AgentSelector)
AGENTS_APPLIED_ROUTE = Route("agents/applied", RecordAppliedArgs)
METRICS_LIST_ROUTE = Route("metrics/list", AgentKeyArgs)
METRICS_PUBLISH_ROUTE = Route("metrics/publish", PublishMetricsArgs)
METRICS_DESTROY_ROUTE = Route("metrics/destroy", DestroyMetricsArgs)

MAX_BODY_BYTES = 1 << 20
"""The largest request body a command route accepts. A code-metric library is
sent in one body, so this also bounds a library's size."""

# The chat agent's A2A app name, the path segment under `/a2a/`. The CLI's `run`
# and the dashboard's chat both reach the chat agent through it.
CHAT_A2A_APP = "aqa_chat"
