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

"""The update trigger: what starts an investigation when an attached agent is updated.

It is an audit log sink on the observed engine's `UpdateReasoningEngine`
entries, publishing to AQuA's ambient topic, and a `roles/pubsub.publisher`
binding on that topic for the sink's writer identity. The sink lives in the
observed engine's project, where its audit entries are written; the topic is
AQuA's.

`attach` reads what is in place on the user's ADC over REST, prints the gcloud
commands that bring it to what the attachment says, and, once confirmed, runs
exactly those with the user's credentials::

    plan_trigger
      ├── GET  logging  projects/<p>/sinks/<sink>
      ├── GET  pubsub   <topic>:getIamPolicy          the publisher binding
      └── GET  logging  projects/<p>/sinks            removal: a shared identity
    apply_trigger
      ├── gcloud logging sinks create | update | delete
      ├── GET  logging  projects/<p>/sinks/<sink>     a new sink's identity
      └── gcloud pubsub topics add- | remove-iam-policy-binding

Nothing is kept on disk. The sink's name is derived from the topic and the
agent key, so any directory can find it again, and `detach` removes it the
same way. A sink of that name that publishes elsewhere belongs to another AQuA
and is never touched: an attach that needs the name stops, and a removal
leaves it.

The publisher binding is removed with the sink only when no other sink in that
project publishes to the topic with the same writer identity: Cloud Logging
can give every sink in a project one shared writer identity.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import shlex
import shutil
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

from ambient_quality_cli.rest_client import ApiError, JsonApi
from ambient_quality_shared.agent_key import is_storage_key

PUBLISHER_ROLE = "roles/pubsub.publisher"
"""The role the sink's writer identity needs on AQuA's topic."""

SINK_SUFFIX = "-updates"
"""The end of every update sink's name."""

SINK_NAME_MAX_LENGTH = 100
"""How long a log sink's name may be."""

WRITER_PLACEHOLDER = "<writer identity of the sink>"
"""Stands in for the identity of a sink that does not exist yet; the sink
reports it once created."""

_LOGGING_API = "https://logging.googleapis.com/v2"
_PUBSUB_API = "https://pubsub.googleapis.com/v1"

_TOPIC_RE = re.compile(r"projects/(?P<project>[^/]+)/topics/(?P<name>[^/]+)")
_ENGINE_ID_RE = re.compile(r"[0-9]+")
# A project id, possibly with a domain prefix (`example.com:project`), or a
# number, as in `grants`.
_PROJECT_RE = re.compile(r"(?:[a-z][a-z0-9.-]*:)?[a-z][a-z0-9-]*|[0-9]+")


class TriggerError(Exception):
    """The update trigger could not be read, planned or changed."""


@dataclasses.dataclass(frozen=True)
class Sink:
    """A log sink, as the Logging API reports it."""

    name: str
    destination: str
    filter: str
    writer_identity: str
    """`serviceAccount:<email>`, the principal the sink publishes as."""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> Sink:
        """Builds a sink from a Logging API `LogSink`.

        Args:
            body: The decoded `LogSink`.

        Returns:
            The sink.
        """
        return cls(
            name=str(body.get("name", "")),
            destination=str(body.get("destination", "")),
            filter=str(body.get("filter", "")),
            writer_identity=str(body.get("writerIdentity", "")),
        )


@dataclasses.dataclass(frozen=True)
class TriggerPlan:
    """What brings one agent's update trigger to what its attachment says."""

    agent_key: str
    project: str
    """The project holding the sink: the observed engine's."""

    sink_name: str
    topic: str
    engine_id: str
    """The engine whose updates the sink matches; empty to remove the sink."""

    existing: Sink | None
    commands: tuple[tuple[str, ...], ...]
    """The gcloud commands, in order. One may hold `WRITER_PLACEHOLDER`."""

    @property
    def is_removal(self) -> bool:
        """Whether the plan removes the trigger rather than creating it."""
        return not self.engine_id

    def render_lines(self) -> list[str]:
        """Renders the plan as lines of the attach or detach plan.

        Returns:
            Lines to print, in order; empty when removing a trigger that does
            not exist.
        """
        where = f"{self.project}/sinks/{self.sink_name}"
        if not self.commands:
            if self.is_removal:
                return []
            return [f"\nUpdate trigger: {where}, in place."]
        what = (
            "remove it"
            if self.is_removal
            else (
                "update it"
                if self.existing
                else f"create it for engine {self.engine_id}"
            )
        )
        return [
            f"\nUpdate trigger: {where}, publishing to {self.topic}. To "
            f"{what}, run with your own credentials:",
            *(f"  {shlex.join(argv)}" for argv in self.commands),
        ]


def parse_topic(topic: str) -> tuple[str, str]:
    """Splits a topic id into its project and its name.

    Args:
        topic: `projects/<project>/topics/<name>`.

    Returns:
        Tuple of (project, topic name).

    Raises:
        TriggerError: If `topic` is not a topic id.
    """
    match = _TOPIC_RE.fullmatch(topic)
    if not match:
        raise TriggerError(
            f"AQuA reports {topic!r} as its topic, which is not a topic id."
        )
    return match["project"], match["name"]


def build_sink_name(topic: str, agent_key: str) -> str:
    """Builds the name of an agent's update sink.

    Prefixed with the topic's name, so two AQuA instances' triggers for one
    agent stay apart in one project, and named by the agent key, so the same
    agent's staging and prod do too. Past `SINK_NAME_MAX_LENGTH`, the name is
    cut and a hash of the whole keeps two cut names apart.

    Args:
        topic: AQuA's topic id.
        agent_key: What the attachment is stored under.

    Returns:
        The sink's name.

    Raises:
        TriggerError: If the topic or the key is malformed.
    """
    _, topic_name = parse_topic(topic)
    if not is_storage_key(agent_key):
        raise TriggerError(f"{agent_key!r} is not an agent key.")
    name = f"{topic_name}-{agent_key}"
    if len(name) + len(SINK_SUFFIX) <= SINK_NAME_MAX_LENGTH:
        return f"{name}{SINK_SUFFIX}"
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    cut = SINK_NAME_MAX_LENGTH - len(SINK_SUFFIX) - len(digest) - 1
    return f"{name[:cut]}-{digest}{SINK_SUFFIX}"


def build_sink_filter(engine_id: str) -> str:
    """Builds the filter that matches one engine's completed updates.

    Args:
        engine_id: The observed engine's numeric id.

    Returns:
        The Logging filter.
    """
    return " AND ".join(
        [
            'protoPayload.serviceName="aiplatform.googleapis.com"',
            # Substring match, for every API version (v1, v1beta1).
            'protoPayload.methodName:"ReasoningEngineService.UpdateReasoningEngine"',
            # Otherwise every engine update in the project matches, AQuA's own
            # redeploys included, and each match buys an investigation.
            # Matched on the id alone because the observed engine's region need
            # not be AQuA's, and the audit entry names the project by number.
            f'protoPayload.resourceName=~"/reasoningEngines/{engine_id}$"',
            # The completion entry, whose revision is live and has traces.
            'operation.last="true"',
            "NOT protoPayload.status.code>0",
        ]
    )


def build_destination(topic: str) -> str:
    """Builds the sink destination that publishes to a topic.

    Args:
        topic: AQuA's topic id.

    Returns:
        The destination, as `pubsub.googleapis.com/<topic>`.
    """
    return f"pubsub.googleapis.com/{topic}"


def get_sink(api: JsonApi, project: str, name: str) -> Sink | None:
    """Reads one sink.

    Args:
        api: Google REST client, with the user's credentials.
        project: The sink's project.
        name: The sink's name.

    Returns:
        The sink, or None when there is none of that name.

    Raises:
        TriggerError: If the sink cannot be read.
    """
    try:
        body = api.get_json(
            f"{_LOGGING_API}/projects/{quote(project)}/sinks/{quote(name)}"
        )
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise TriggerError(
            f"Could not read the sink {name} in {project}: {exc}"
        ) from exc
    return Sink.from_api(body)


def list_sinks(api: JsonApi, project: str) -> list[Sink]:
    """Lists every sink in a project.

    Args:
        api: Google REST client, with the user's credentials.
        project: The project.

    Returns:
        The sinks.

    Raises:
        TriggerError: If the sinks cannot be listed.
    """
    sinks: list[Sink] = []
    token = ""
    while True:
        try:
            body = api.get_json(
                f"{_LOGGING_API}/projects/{quote(project)}/sinks",
                {"pageToken": token} if token else None,
            )
        except ApiError as exc:
            raise TriggerError(
                f"Could not list the sinks in {project}: {exc}"
            ) from exc
        sinks.extend(Sink.from_api(s) for s in body.get("sinks") or [])
        token = str(body.get("nextPageToken") or "")
        if not token:
            return sinks


def has_publisher_binding(api: JsonApi, topic: str, member: str) -> bool | None:
    """Checks whether a member may publish to the topic.

    Args:
        api: Google REST client, with the user's credentials.
        topic: AQuA's topic id.
        member: `serviceAccount:<email>`.

    Returns:
        Whether the topic's policy binds `member` to `PUBLISHER_ROLE`, or
        None when the policy cannot be read.
    """
    try:
        policy = api.get_json(f"{_PUBSUB_API}/{topic}:getIamPolicy")
    except ApiError:
        return None
    return any(
        binding.get("role") == PUBLISHER_ROLE
        and member in (binding.get("members") or [])
        and not binding.get("condition")
        for binding in policy.get("bindings") or []
    )


def plan_trigger(
    api: JsonApi,
    *,
    topic: str,
    agent_key: str,
    project: str,
    engine_id: str,
) -> TriggerPlan:
    """Plans the commands that bring an agent's update trigger to what is wanted.

    Steps:
    1. Read the sink of the agent's name in the observed project. One that
       publishes to another topic is another AQuA's: refuse it when the sink
       is wanted, and leave it out of a removal.
    2. For an engine id, create the sink, or update its filter, and bind its
       writer identity on the topic unless that binding is already there.
    3. Without one, remove the sink if it exists, and its binding unless
       another sink of that project still publishes to the topic as the same
       identity.

    Args:
        api: Google REST client, with the user's credentials.
        topic: AQuA's topic id.
        agent_key: What the attachment is stored under.
        project: The observed engine's project, which holds the sink.
        engine_id: The observed engine's numeric id; empty to remove.

    Returns:
        The plan; its commands are empty when nothing needs to change.

    Raises:
        TriggerError: If a value is malformed, the sink cannot be read, or a
            sink of the agent's name publishes elsewhere while `engine_id`
            asks for one.
    """
    sink_name = build_sink_name(topic, agent_key)
    if not _PROJECT_RE.fullmatch(project):
        raise TriggerError(f"{project!r} is not a project id or number.")
    if engine_id and not _ENGINE_ID_RE.fullmatch(engine_id):
        raise TriggerError(f"{engine_id!r} is not a numeric engine id.")
    destination = build_destination(topic)
    existing = get_sink(api, project, sink_name)
    if existing and existing.destination != destination and not engine_id:
        existing = None
    elif existing and existing.destination != destination:
        raise TriggerError(
            f"The sink {sink_name} in {project} publishes to "
            f"{existing.destination}, not to this AQuA's {topic}; leaving it "
            "alone."
        )

    commands: list[tuple[str, ...]] = []
    if engine_id:
        log_filter = f"--log-filter={build_sink_filter(engine_id)}"
        if existing is None:
            commands.append(
                (
                    "gcloud",
                    "logging",
                    "sinks",
                    "create",
                    sink_name,
                    destination,
                    f"--project={project}",
                    log_filter,
                )
            )
        elif existing.filter != build_sink_filter(engine_id):
            commands.append(
                (
                    "gcloud",
                    "logging",
                    "sinks",
                    "update",
                    sink_name,
                    destination,
                    f"--project={project}",
                    log_filter,
                )
            )
        writer = existing.writer_identity if existing else ""
        if not writer or not has_publisher_binding(api, topic, writer):
            commands.append(
                _build_binding_argv("add", topic, writer or WRITER_PLACEHOLDER)
            )
    elif existing is not None:
        writer = existing.writer_identity
        if (
            writer
            and has_publisher_binding(api, topic, writer) is not False
            and not _is_writer_shared(api, project, existing, destination)
        ):
            commands.append(_build_binding_argv("remove", topic, writer))
        commands.append(
            (
                "gcloud",
                "logging",
                "sinks",
                "delete",
                sink_name,
                f"--project={project}",
                "--quiet",
            )
        )
    return TriggerPlan(
        agent_key=agent_key,
        project=project,
        sink_name=sink_name,
        topic=topic,
        engine_id=engine_id,
        existing=existing,
        commands=tuple(commands),
    )


def _is_writer_shared(
    api: JsonApi, project: str, sink: Sink, destination: str
) -> bool:
    """Checks whether another sink publishes to the topic as the same identity.

    Args:
        api: Google REST client, with the user's credentials.
        project: The sink's project.
        sink: The sink being removed.
        destination: The topic's sink destination.

    Returns:
        Whether removing the binding would stop another sink publishing.
    """
    return any(
        other.name != sink.name
        and other.destination == destination
        and other.writer_identity == sink.writer_identity
        for other in list_sinks(api, project)
    )


def _build_binding_argv(
    action: str, topic: str, member: str
) -> tuple[str, ...]:
    """Builds the command that adds or removes the publisher binding.

    Args:
        action: `add` or `remove`.
        topic: AQuA's topic id.
        member: The sink's writer identity, or `WRITER_PLACEHOLDER`.

    Returns:
        The command.
    """
    return (
        "gcloud",
        "pubsub",
        "topics",
        f"{action}-iam-policy-binding",
        topic,
        f"--member={member}",
        f"--role={PUBLISHER_ROLE}",
    )


Runner = Callable[[list[str]], int]
"""Runs one command and returns its exit status."""


def apply_trigger(
    plan: TriggerPlan,
    *,
    api: JsonApi,
    runner: Runner,
    echo: Callable[[str], None],
    which: Callable[[str], str | None] | None = None,
) -> list[list[str]]:
    """Runs a plan's commands, as printed, with the user's credentials.

    A command that binds `WRITER_PLACEHOLDER` gets the identity the sink
    reports once the command before it has created it.

    Args:
        plan: What `plan_trigger` returned.
        api: Google REST client, with the user's credentials.
        runner: Runs one command.
        echo: Prints one line.
        which: Resolves a program on PATH; `shutil.which` when None.

    Returns:
        The commands that ran, as run.

    Raises:
        TriggerError: If gcloud is missing, a command fails, or the created
            sink reports no writer identity.
    """
    if plan.commands and (which or shutil.which)("gcloud") is None:
        raise TriggerError(
            "Not on PATH: gcloud. Install the Google Cloud CLI, or run the "
            "commands above wherever it is installed."
        )
    ran: list[list[str]] = []
    for command in plan.commands:
        argv = list(command)
        if any(WRITER_PLACEHOLDER in arg for arg in argv):
            argv = _fill_writer_identity(argv, plan, api)
        echo(f"$ {shlex.join(argv)}")
        if status := runner(argv):
            raise TriggerError(
                _render_failure(
                    f"Exit status {status} from: {shlex.join(argv)}", ran
                )
            )
        ran.append(argv)
    return ran


def _fill_writer_identity(
    argv: Sequence[str], plan: TriggerPlan, api: JsonApi
) -> list[str]:
    """Replaces `WRITER_PLACEHOLDER` with the identity the created sink reports.

    Args:
        argv: The command, holding the placeholder.
        plan: The plan the command is from.
        api: Google REST client, with the user's credentials.

    Returns:
        The command with the identity filled in.

    Raises:
        TriggerError: If the sink is missing or reports no identity.
    """
    sink = get_sink(api, plan.project, plan.sink_name)
    if sink is None or not sink.writer_identity:
        raise TriggerError(
            f"The sink {plan.sink_name} in {plan.project} reports no writer "
            f"identity, so it cannot be granted {PUBLISHER_ROLE} on "
            f"{plan.topic}."
        )
    return [
        arg.replace(WRITER_PLACEHOLDER, sink.writer_identity) for arg in argv
    ]


def _render_failure(message: str, ran: Sequence[Sequence[str]]) -> str:
    """Renders a failed command, with what ran before it.

    Args:
        message: The failure.
        ran: Commands that succeeded before it.

    Returns:
        The message, noting that a rerun plans from what the earlier commands
        left, since the plan is read from what exists.
    """
    if not ran:
        return message
    return (
        f"{message}\nThe commands before it ran; running the same command "
        "again plans from what they left."
    )
