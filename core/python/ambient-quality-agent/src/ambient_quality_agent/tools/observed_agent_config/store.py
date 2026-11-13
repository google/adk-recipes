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

"""How an observed agent is watched, stored in the jobs store.

One object per attached agent is stored at `agents/<agent key>.json` in the
`ObjectStore` that `objects.store.jobs_store_factory` returns: the jobs bucket in
a deployment, `.aqua/job/` in a standalone run. The agent key
(`ambient_quality_shared.agent_key`) is `<project id>__<agent name>`, so the same
agent in two projects has two objects; an agent in AQuA's own project keeps its
bare name (`keys`).
The jobs bucket is reused because
the engine service account has read/write permissions and the UI service account
has read permissions. Only the engine writes these objects: `aqua attach` sends
its changes to the command routes backed by `commands`, so no other principal
needs write access to the bucket.

The configuration object is authoritative for an attached agent. Omitted keys
fall back to `ObservedAgentConfig` defaults.

The envelope schema is versioned and forward-tolerant. Deployments read objects
written by various CLI versions, so unrecognized keys are logged as warnings and
omitted rather than raising errors that would disrupt requests.

Infrastructure fields (`observed_engine_id` and the agent's
`observed_project_id`) are wired into the audit log sink by `attach`.
The `applied` section records values pushed during the last apply to
distinguish desired configuration from active infrastructure state.

The schedule (`investigation_schedule`, `scheduled_trigger_enabled`) is read
from this object by the engine on every scheduler tick (`schedule`), for every
attached agent, so a write takes effect on the next tick. It is always a cron
expression, never empty, so that a reader of the object knows when the agent
is investigated. A schedule stored empty reads as
`DEFAULT_INVESTIGATION_SCHEDULE`, the schedule of an agent with no object.

The `applied` section also lists the IAM grants `attach` ran on the
observed agent's resources, so that `detach` revokes only bindings
that a grant command of `attach` was run for.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_shared.agent_key import SEPARATOR, extract_agent_name

if TYPE_CHECKING:
    from ambient_quality_agent.tools.objects.store import ObjectStore

logger = logging.getLogger(__name__)

AGENTS_PREFIX = "agents/"
"""Prefix under which agent configuration objects are stored."""

SCHEMA_VERSION = 1
"""Schema version of the agent configuration envelope."""

DEFAULT_INVESTIGATION_SCHEDULE = "0 12 * * *"
"""The scheduled sweep's cron when none is set: daily at 12:00 UTC."""

TRIGGER_FIELDS = (
    "observed_engine_id",
    "observed_project_id",
)
"""Fields the update trigger acts on, which `attach` wires with gcloud.

`observed_project_id` is a runtime field of `ObservedAgentConfig` as well,
since ingestion reads the agent's telemetry from that project."""


@dataclass(frozen=True)
class AppliedGrant:
    """One IAM binding `attach` made for AQuA's service account."""

    resource: str
    """What it grants access to, as the CLI names it: `project:dataset`,
    `gs://bucket` or `projects/project`."""

    role: str
    """The role granted."""

    member: str
    """The principal granted, as `serviceAccount:<email>`."""

    @property
    def key(self) -> tuple[str, str, str]:
        """The binding: resource, role and member."""
        return (self.resource, self.role, self.member)


GRANT_FIELDS = frozenset(f.name for f in dataclasses.fields(AppliedGrant))
"""The keys of one recorded grant."""


def is_grant_record(fields: Any) -> bool:
    """Checks that a value is one grant as recorded: exactly its three keys.

    Args:
        fields: Candidate value.

    Returns:
        True if `fields` maps each of `GRANT_FIELDS`, and nothing else, to a
        non-empty string.
    """
    return (
        isinstance(fields, dict)
        and set(fields) == GRANT_FIELDS
        and all(isinstance(v, str) and v for v in fields.values())
    )


@dataclass(frozen=True)
class Applied:
    """Configuration values `attach` applied outside the engine.

    Serves as an advisory record of applied state rather than reading back
    from the audit sink directly.
    """

    at: str = ""
    """When the triggers were last applied, in ISO 8601 format. Empty when
    only grants are recorded: an attach whose trigger commands failed still ran
    its grants."""

    observed_engine_id: str = ""
    observed_project_id: str = ""

    grants: tuple[AppliedGrant, ...] = ()
    """The grants `attach` ran and no `detach` has revoked."""


@dataclass(frozen=True)
class AgentRecord:
    """An attached agent's configuration object and envelope metadata."""

    config: ObservedAgentConfig
    """Runtime configuration for the observed agent, merged via `copy_with_agent`."""

    default: bool = False
    """Whether this agent serves unaddressed requests. Ties resolve alphabetically."""

    observed_deployment_name: str = ""
    """Deployment name of the observed agent in agents-cli."""

    investigation_schedule: str = DEFAULT_INVESTIGATION_SCHEDULE
    """Cron expression for scheduled sweeps, in UTC. The engine reads it on
    every scheduler tick."""

    scheduled_trigger_enabled: bool = True
    """Whether scheduled sweeps run. Updates to the observed agent trigger an
    investigation either way."""

    observed_engine_id: str = ""
    """Engine ID whose events trigger investigations, wired by `attach`."""

    telemetry_payload_bucket: str = ""
    """Bucket the agent uploads message content to, which its telemetry rows
    point into. Not read by ingestion, which follows the `gs://` references in
    the rows; the access check reads it so that AQuA can be granted the bucket
    before the agent has any rows. Stored rather than passed per request, so an
    agent with no engine to discover it from (Cloud Run, GKE), or an attach
    with `--no-discover`, keeps it without the flag being given again."""

    updated_at: str = ""
    """Timestamp of the last record write in ISO 8601 format."""

    applied: Applied | None = None
    """Applied infrastructure state, or `None` if `attach` has wired nothing."""

    @property
    def agent_name(self) -> str:
        """The agent's root agent name, which its telemetry is filtered by."""
        return self.config.observed_agent_name

    @property
    def agent_key(self) -> str:
        """The key the record is stored under."""
        return self.config.agent_key

    def list_unapplied_fields(self) -> list[str]:
        """Lists update trigger fields whose configured value differs from the applied state.

        All fields are considered pending until the first apply of the
        triggers completes, distinguishing desired configuration from active
        infrastructure.

        Returns:
            Names of fields pending synchronization by `attach`.
        """
        if self.applied is None or not self.applied.at:
            return list(TRIGGER_FIELDS)
        configured = self.extract_trigger_values()
        return [
            name
            for name in TRIGGER_FIELDS
            if configured[name] != getattr(self.applied, name)
        ]

    def extract_trigger_values(self) -> dict[str, Any]:
        """Extracts the values `attach` wires the update trigger with.

        Returns:
            Each of `TRIGGER_FIELDS`, from the record or from its
            runtime configuration.
        """
        return {
            name: getattr(self.config if name in _CONFIG_FIELDS else self, name)
            for name in TRIGGER_FIELDS
        }


_CONFIG_FIELDS = frozenset(
    f.name for f in dataclasses.fields(ObservedAgentConfig)
)
RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "default",
        "observed_deployment_name",
        "investigation_schedule",
        "scheduled_trigger_enabled",
        "observed_engine_id",
        "telemetry_payload_bucket",
    }
)
"""`AgentRecord` fields stored in the `agent` section beside the runtime fields."""

_APPLIED_FIELDS = frozenset(f.name for f in dataclasses.fields(Applied))

_IGNORED_APPLIED_FIELDS = frozenset(
    {"investigation_schedule", "scheduled_trigger_enabled"}
)
"""Schedule keys an `applied` section may carry.

Read without a warning and dropped: the schedule in effect is the one in the
`agent` section."""


def build_object_name(agent_key: str) -> str:
    """Builds the object name for an agent.

    Slashes are replaced with underscores to prevent escaping the agents
    prefix and overwriting unrelated objects in the store.

    Args:
        agent_key: Key of the agent.

    Returns:
        The object name.
    """
    return f"{AGENTS_PREFIX}{agent_key.replace('/', '_')}.json"


def get_agent_record(store: ObjectStore, agent_key: str) -> AgentRecord | None:
    """Fetches the configuration record for an attached agent.

    Missing objects return `None` because unattached agents are expected in normal
    deployments. Parse and storage errors are propagated to the caller.

    Args:
        store: Store containing agent configurations.
        agent_key: Key of the agent to look up.

    Returns:
        The parsed agent record, or `None` if the agent is not attached.
    """
    raw = store.read_text(build_object_name(agent_key))
    if raw is None:
        return None
    return parse_agent_record(raw, agent_key)


def save_agent_record(store: ObjectStore, record: AgentRecord) -> AgentRecord:
    """Saves an agent configuration record with a current UTC timestamp.

    Overwrites the entire storage object because the record is authoritative
    for the agent and does not merge with prior state.

    Args:
        store: Store where the configuration is kept.
        record: Agent configuration record to save.

    Returns:
        The saved agent record with its updated timestamp.

    Raises:
        ValueError: If the record has no agent key to be stored under.
    """
    if not record.agent_key:
        raise ValueError(f"the record for {record.agent_name!r} has no key")
    stamped = dataclasses.replace(
        record, updated_at=dt.datetime.now(dt.UTC).isoformat()
    )
    store.write_text(
        build_object_name(stamped.agent_key),
        json.dumps(agent_record_to_json(stamped), indent=2),
    )
    return stamped


def delete_agent_record(store: ObjectStore, agent_key: str) -> bool:
    """Deletes the configuration object for an agent.

    An absent object is the state the caller asks for, so it is not an error.

    Args:
        store: Store where the configuration is kept.
        agent_key: Key of the observed agent.

    Returns:
        True if an object was deleted, False if none existed.
    """
    return store.delete(build_object_name(agent_key))


def list_agents(store: ObjectStore) -> list[str]:
    """Lists all attached agent keys in alphabetical order.

    Extracts keys from object names without downloading contents, keeping
    the listing to a single storage request. An object stored under a bare
    agent name is listed under it.

    Args:
        store: Store containing agent configurations.

    Returns:
        Sorted list of attached agent keys.
    """
    names = []
    for object_name in store.list_names(AGENTS_PREFIX):
        name = object_name[len(AGENTS_PREFIX) :]
        if name.endswith(".json") and "/" not in name:
            names.append(name[: -len(".json")])
    return sorted(names)


def resolve_default(
    store: ObjectStore, pending: AgentRecord | None = None
) -> str | None:
    """Resolves the default agent for requests that specify no agent.

    Resolution order:
    1. Filter attached agents whose `default` flag is true.
    2. Fall back to all attached agents if no agent is explicitly flagged.
    3. Choose the alphabetically first candidate, ensuring deterministic
       resolution without modifying sibling records.

    Args:
        store: Store containing agent configurations.
        pending: A record not yet saved, which takes the place of its agent's
            stored record.

    Returns:
        The resolved agent key, or `None` if no agents are attached.
    """
    names = list_agents(store)
    if pending is not None and pending.agent_key not in names:
        names = sorted([*names, pending.agent_key])
    flagged = []
    for name in names:
        if pending is not None and name == pending.agent_key:
            record: AgentRecord | None = pending
        else:
            record = get_agent_record(store, name)
        if record is not None and record.default:
            flagged.append(name)
    candidates = flagged or names
    return candidates[0] if candidates else None


def agent_record_to_json(record: AgentRecord) -> dict[str, Any]:
    """Serializes an agent record into storage dictionary format.

    The `agent` section flattens runtime and update trigger fields together
    so callers inspect a unified configuration without distinguishing backends.

    Args:
        record: The agent record to serialize.

    Returns:
        JSON-serializable dictionary representing the configuration object.
    """
    agent: dict[str, Any] = dataclasses.asdict(record.config)
    agent.update(
        {name: getattr(record, name) for name in sorted(RECORD_FIELDS)}
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": record.updated_at,
        "agent": agent,
    }
    if record.applied is not None:
        payload["applied"] = dataclasses.asdict(record.applied)
    return payload


def parse_agent_record(raw: bytes | str, agent_key: str) -> AgentRecord:
    """Parses a stored configuration object into an AgentRecord.

    Parsing sequence:
    1. Validate that the payload is a valid JSON dictionary.
    2. Warn if the schema version is newer than supported.
    3. Extract recognized fields, enforcing `agent_key` and the agent's name
       from the storage key to prevent identity mismatches. An object stored
       under a bare agent name is named by it.
    4. Parse the optional applied state.
    5. Construct and return the `AgentRecord`.

    Args:
        raw: Raw JSON payload as bytes or string.
        agent_key: Agent key derived from the storage key.

    Returns:
        Parsed `AgentRecord` containing known fields.

    Raises:
        ValueError: If `raw` is not a JSON object or lacks an `agent` section.
    """
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"agent config for {agent_key!r} is not a JSON object")

    version = payload.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, int) and version > SCHEMA_VERSION:
        logger.warning(
            "agent config for %s is schema version %d, newer than the %d this "
            "build knows; reading the fields it recognizes.",
            agent_key,
            version,
            SCHEMA_VERSION,
        )

    agent = payload.get("agent")
    if not isinstance(agent, dict):
        raise ValueError(f"agent config for {agent_key!r} has no agent section")

    # The `agent` section holds both field sets side by side, so its unknown
    # keys are the ones outside their union. Checking each set on its own would
    # report every field of the other set as unknown.
    _warn_unknown_fields(
        agent, _CONFIG_FIELDS | RECORD_FIELDS, agent_key, "agent"
    )
    config_fields = _extract_fields(agent, _CONFIG_FIELDS)
    config_fields["agent_key"] = agent_key
    config_fields["observed_agent_name"] = _resolve_agent_name(
        str(config_fields.get("observed_agent_name") or ""), agent_key
    )
    record_fields = _resolve_schedule(_extract_fields(agent, RECORD_FIELDS))

    applied = payload.get("applied")
    applied_state = None
    if isinstance(applied, dict):
        _warn_unknown_fields(
            applied,
            _APPLIED_FIELDS | _IGNORED_APPLIED_FIELDS,
            agent_key,
            "applied",
        )
        applied_fields = _extract_fields(applied, _APPLIED_FIELDS)
        applied_fields["grants"] = _parse_grants(
            applied_fields.get("grants"), agent_key
        )
        applied_state = Applied(**applied_fields)

    return AgentRecord(
        config=ObservedAgentConfig(**config_fields),
        updated_at=str(payload.get("updated_at", "")),
        applied=applied_state,
        **record_fields,
    )


def _resolve_schedule(fields: dict[str, Any]) -> dict[str, Any]:
    """Resolves an empty stored schedule to the default one.

    The engine runs an object that stores an empty schedule on the default,
    so the object reads as storing it.

    Args:
        fields: Fields read from one section of the object.

    Returns:
        `fields`, with `DEFAULT_INVESTIGATION_SCHEDULE` in place of an empty
        `investigation_schedule`.
    """
    if fields.get("investigation_schedule") == "":
        return {
            **fields,
            "investigation_schedule": DEFAULT_INVESTIGATION_SCHEDULE,
        }
    return fields


def _resolve_agent_name(stored_name: str, agent_key: str) -> str:
    """Resolves an attachment's agent name from its object and its key.

    The key decides, so that an object cannot claim another agent. A bare name
    may itself contain `__` (`support__bot`), so the name the object records
    is taken whenever the key is that name, or ends with it.

    Args:
        stored_name: The `observed_agent_name` the object records; empty if
            none.
        agent_key: The key the object is stored under.

    Returns:
        The agent's name.
    """
    if stored_name and (
        agent_key == stored_name
        or agent_key.endswith(f"{SEPARATOR}{stored_name}")
    ):
        return stored_name
    return extract_agent_name(agent_key)


def _parse_grants(raw: Any, agent_key: str) -> tuple[AppliedGrant, ...]:
    """Parses the recorded grants, dropping any entry that is not one.

    A dropped entry is logged rather than raised: the rest of the object still
    says how the agent is observed, and a grant whose binding cannot be read
    could not be revoked anyway.

    Args:
        raw: The `applied.grants` value as stored; absent is None.
        agent_key: Agent key for diagnostic log messages.

    Returns:
        The grants, in the order they were recorded.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning(
            "agent config for %s has applied.grants that is not a list; "
            "ignoring it.",
            agent_key,
        )
        return ()
    grants = []
    for entry in raw:
        # Keys beyond the three are tolerated, like anywhere else in the
        # envelope: a newer build may record more about a grant.
        fields = (
            _extract_fields(entry, GRANT_FIELDS)
            if isinstance(entry, dict)
            else {}
        )
        if is_grant_record(fields):
            grants.append(AppliedGrant(**fields))
        else:
            logger.warning(
                "agent config for %s has a recorded grant this build cannot "
                "read: %r; ignoring it.",
                agent_key,
                entry,
            )
    return tuple(grants)


def _extract_fields(
    section: dict[str, Any], allowed: frozenset[str]
) -> dict[str, Any]:
    """Filters a dictionary to the allowed keys.

    Args:
        section: Source dictionary of configuration fields.
        allowed: Set of permitted field names.

    Returns:
        Dictionary containing only permitted keys and their values.
    """
    return {key: value for key, value in section.items() if key in allowed}


def _warn_unknown_fields(
    section: dict[str, Any],
    recognized: frozenset[str],
    agent_key: str,
    where: str,
) -> None:
    """Logs a warning naming the keys of `section` that this build ignores.

    The warning distinguishes a typo from a field added by a newer CLI, which
    both look like a setting that did not apply. `recognized` must hold every
    key the section may carry: a warning that fires on valid objects hides the
    ones that matter.

    Args:
        section: Source dictionary of configuration fields.
        recognized: Every field name the section may carry.
        agent_key: Agent key for diagnostic log messages.
        where: Section name in the configuration object for diagnostic log messages.
    """
    unknown = sorted(set(section) - recognized)
    if unknown:
        logger.warning(
            "agent config for %s has %s key(s) this build does not know: %s; "
            "ignoring them.",
            agent_key,
            where,
            ", ".join(unknown),
        )
