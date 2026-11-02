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

"""What `aqua attach`, `list-agents` and `detach` do, behind command routes.

The CLI talks to AQuA only through its API, so it never touches the jobs
store. It sends the fields it wants changed; this module merges them over what
the agent is observed with now, validates the result, and stores it with
`store` in the jobs store (`objects.store.jobs_store_factory`): the jobs bucket
in a deployment, `.aqua/job/` in a standalone run. The engine's service account
already reads and writes the jobs bucket, and no other principal needs to.

**Validation is the runtime's own.** The merged fields become an
`ObservedAgentConfig` and pass through `Config.copy_with_agent`, the same gate
the stored object goes through when a request reads it, and what is stored is
extracted back out of the result. A value the runtime would clamp is stored
clamped, and the plan reports it.

**Which attachment an attach writes** is named by the agent key,
`<project id>__<agent name>` (`ambient_quality_shared.agent_key`), chosen here
from the agent's name and the project it runs in -- the requested
`observed_project_id`, or AQuA's own -- by the rules of `keys`: an agent in
AQuA's own project keeps its bare name. The same
agent in two projects is two attachments. A request may name an existing
attachment by its key instead. An attach that would move an attachment to
another project or re-point it at another engine is refused rather than
silently taking over its history.

**What an attach is merged over** is whatever the agent is observed with now:
the stored object when there is one, and the defaults otherwise. An attach that
sets one flag therefore changes one field, and the agent stays on the telemetry
table it is read from.

**Whether AQuA can read it** is checked by a dry run that asks for it, with
`access_check` on the merged fields. The check runs here, in the engine, so it
reads as AQuA's service account rather than as whoever runs the CLI.

**What the table decides**, its location and source, is filled in before the
check by a dry run that asks for it, wherever the request leaves it out. The
plan names those values in `filled`, and the caller sends them back in
`fields` to store them: storing reads nothing from BigQuery.

**What the update trigger is wired to** is decided here too. The attach
response carries the values of the fields the audit sink acts on, as merged,
and the engine's own trigger target: the topic the sink publishes to, with its
region, resource name and caller account, which name the instance. `attach`
wires exactly those with gcloud, so it needs nothing of AQuA but the resource
name it called, then reports back with `record_applied` so the object says
what is in effect.

**Which grants to revoke** is recorded the same way. `attach` runs the grant
commands with the user's credentials and reports each one it ran to
`record_applied`; `list_agents` returns them, and `detach` revokes those and
reports them back as revoked. It revokes no binding that a grant
command of `attach` was not run for.

The chat agent holds none of these functions: changing how an agent is observed
does not depend on a model choosing to do it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import re
from typing import Any

from ambient_quality_agent import config as config_module
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.config import config as env_config
from ambient_quality_agent.tools.evaluation.models import (
    MULTI_TURN_METRICS,
    SINGLE_TURN_METRICS,
)
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.observed_agent_config import (
    access_check,
    effective_config,
    keys,
    schedule,
    store,
)
from ambient_quality_shared.agent_key import is_storage_key

logger = logging.getLogger(__name__)

_CONFIG_FIELDS = {
    f.name: f.type
    for f in dataclasses.fields(ObservedAgentConfig)
    if f.name not in {"observed_agent_name", "agent_key"}
}
"""The runtime fields an attach may set, with each field's annotation.

The request names `observed_agent_name` separately, and `agent_key` is given
in the request or resolved from the name and the project.
"""

_RECORD_FIELDS = {
    f.name: f.type
    for f in dataclasses.fields(store.AgentRecord)
    if f.name in store.RECORD_FIELDS
}
"""The attachment fields, which only the stored object carries."""

_APPLIED_FIELDS = {
    f.name: f.type
    for f in dataclasses.fields(store.Applied)
    if f.name in store.TRIGGER_FIELDS
}
"""The fields the update trigger acts on, as `record_applied` takes them."""

SETTABLE_FIELDS = frozenset(_CONFIG_FIELDS) | frozenset(_RECORD_FIELDS)
"""Every field an attach request may set."""

_BUCKET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
"""A Cloud Storage bucket name, the shape `telemetry_payload_bucket` takes.

The access check puts it into a request path unquoted, and a grant command
built from it accepts no other shape."""

_NO_BUCKET = (
    "This deployment has no jobs bucket, so there is nowhere to store an "
    "attachment. Set AQA_JOBS_GCS_BUCKET, or run AQuA standalone."
)

_KNOWN_METRICS = {
    "multi_turn_metrics": MULTI_TURN_METRICS.keys(),
    "single_turn_metrics": SINGLE_TURN_METRICS.keys(),
}
"""The prebuilt metrics each metric field accepts. The runtime drops any other
name, so an attach refuses one rather than store a set nobody asked for."""


async def attach_agent(
    *,
    agent_name: str,
    fields: dict[str, Any],
    dry_run: bool,
    check_access: bool = False,
    fill_from_table: bool = False,
    agent_key: str = "",
) -> dict[str, Any]:
    """Merges `fields` over how an agent is observed now, and stores the result.

    Steps:
    1. Refuse a request that names an unknown field or a value of the wrong type.
    2. Resolve the agent key: the one given, or chosen from the agent's name
       and the project it runs in (`keys.resolve_stored_key`).
    3. Read the stored object. Refuse a given key with no attachment, a
       resolved key that is not well formed, or a name the stored attachment
       does not hold; without an object, start from the defaults.
    4. Merge the fields and validate the result through `Config.copy_with_agent`.
    5. Refuse a merge that changes the key, or re-points an attachment at
       another engine.
    6. For a dry run that checks access and asks, fill in the settings the
       telemetry table decides otherwise and `fields` leaves out.
    7. Compute the plan: each changed field with its value before and after.
    8. For a dry run that asks, check that AQuA can read the telemetry the
       result points at.
    9. Store the result, unless this is a dry run or an attached agent does not
       change.

    Args:
        agent_name: Agent to attach. Empty means the agent `agent_key` names.
        fields: Settable fields to change, keyed by field name.
        dry_run: Whether to return the plan without storing anything.
        check_access: Whether a dry run also checks, as AQuA's service account,
            the reads an investigation of the result would make. Ignored when
            storing, which the caller does after a dry run that checked.
        fill_from_table: Whether a dry run that checks access first fills in,
            from the telemetry table's metadata, the settings it decides
            otherwise (`access_check.read_table_settings`) that `fields` leaves
            out. Ignored otherwise. The caller stores them by sending them in
            `fields`.
        agent_key: Key of an existing attachment to change. Empty to build the
            key from the agent's name and its project.

    Returns:
        The plan and whether the object was written, with `filled` naming the
        settings filled in from the table, each with its value and reason, and
        `access` holding the check's results when it ran; or `{"error": ...}`
        when the request is refused or storage fails.
    """
    agent_key = agent_key.strip()
    name = agent_name.strip()
    if refusal := _validate_command(name or agent_key, fields):
        return {"error": refusal}
    jobs = objects.jobs_store_factory()
    # Named in the messages below even if resolving the key fails.
    key = agent_key or name
    try:
        key = agent_key or await asyncio.to_thread(
            _resolve_stored_key, jobs, name, fields
        )
        stored = await asyncio.to_thread(store.get_agent_record, jobs, key)
    except objects.StorageNotConfiguredError:
        return {"error": _NO_BUCKET}
    except Exception as exc:
        logger.warning(
            "attach: reading the stored object for %s failed",
            key,
            exc_info=True,
        )
        return {
            "error": f"Could not read the stored attachment for {key}: {exc}"
        }
    if agent_key and stored is None:
        return {"error": f"No agent is attached as {agent_key!r}."}
    # A key given names an existing attachment, possibly one under a bare name.
    if not agent_key and (refusal := _validate_agent_key(key)):
        return {"error": refusal}
    if stored is not None and name and name != stored.agent_name:
        return {
            "error": f"{key} is attached for {stored.agent_name}, not {name}."
        }

    before = stored or await asyncio.to_thread(
        _build_unattached_record, name, key
    )
    try:
        after = _merge_fields(before, fields)
    except (TypeError, ValueError) as exc:
        return {"error": f"Refusing to attach {key}: {exc}"}
    if stored is not None and (refusal := _validate_merged_key(stored, after)):
        return {"error": refusal}
    filled: list[access_check.TableSetting] = []
    if dry_run and check_access and fill_from_table:
        filled = [
            setting
            for setting in await asyncio.to_thread(
                access_check.read_table_settings,
                after.config,
                project_id=env_config.project_id,
            )
            if setting.field not in fields
        ]
        if filled:
            after = _merge_fields(after, {s.field: s.value for s in filled})

    changes = _compute_changes(before, after)
    is_default = await asyncio.to_thread(_is_default_once_saved, jobs, after)
    result: dict[str, Any] = {
        "agent_key": key,
        "agent_name": after.agent_name,
        "object": jobs.uri(store.build_object_name(key)),
        "attached": stored is not None,
        # Whether a request that selects no agent gets this one. A CLI that
        # does not read `triggers_name_agent` wires triggers only for it.
        "watched": is_default,
        # Each trigger investigates its own agent, so `attach` wires any
        # agent's update trigger. A CLI wires none for an agent besides the
        # default against an engine that does not report this.
        "triggers_name_agent": True,
        "changes": changes,
        "adjusted": _compute_adjustments(fields, after),
        "filled": [dataclasses.asdict(setting) for setting in filled],
        "needs_apply": [
            c["field"] for c in changes if c["field"] in store.TRIGGER_FIELDS
        ],
        # What `attach` wires the update trigger with, and where to.
        "triggers": after.extract_trigger_values(),
        # Which of those the trigger does not act on yet, so that `attach`
        # records what it wired only when that changes the record.
        "unapplied_fields": after.list_unapplied_fields(),
        "trigger_target": build_trigger_target(),
        "written": False,
    }
    if dry_run and check_access:
        # The fields about to be stored, not the stored ones: this is what an
        # investigation will read once the attach goes through.
        result["access"] = await asyncio.to_thread(
            access_check.check_access,
            after.config,
            project_id=env_config.project_id,
            payload_bucket=after.telemetry_payload_bucket,
        )
    # A first attach is written even when it changes nothing: storing it is what
    # attaches the agent.
    if dry_run or (stored is not None and not changes):
        return result

    try:
        saved = await asyncio.to_thread(store.save_agent_record, jobs, after)
    except Exception as exc:
        logger.warning(
            "attach: writing the object for %s failed", key, exc_info=True
        )
        return {"error": f"Could not store the attachment for {key}: {exc}"}
    effective_config.forget_cached_record(key)
    result["written"] = True
    result["updated_at"] = saved.updated_at
    return result


def _resolve_stored_key(
    jobs: objects.ObjectStore, name: str, fields: dict[str, Any]
) -> str:
    """Chooses the key of the attachment an attach without a key writes.

    A request that names no project means the attachment the agent already
    has under its bare name, whichever project that one records, so that
    changing one setting does not start a second attachment.

    Args:
        jobs: Store holding the attachments.
        name: The agent's root agent name.
        fields: Requested fields; `observed_project_id` names the agent's
            project.

    Returns:
        The bare agent name when the request names no project and an
        attachment is stored under it; otherwise the agent key, or the bare
        agent name, as `keys.resolve_stored_key` chooses for the project the
        agent runs in.
    """
    if "observed_project_id" not in fields and store.get_agent_record(
        jobs, name
    ):
        return name
    project = keys.resolve_key_project(
        str(fields.get("observed_project_id") or ""), env_config
    )
    return keys.resolve_stored_key(
        name,
        project,
        config=env_config,
        lookup=lambda key: store.get_agent_record(jobs, key),
    )


def _validate_agent_key(key: str) -> str:
    """Checks that a chosen key is one every place that holds a key accepts.

    Args:
        key: Agent key, or a bare agent name.

    Returns:
        The reason the key is refused, or an empty string if it is not.
    """
    if is_storage_key(key):
        return ""
    return (
        f"Cannot key an attachment as {key!r}: it needs an agent name that is a "
        "Python identifier and, for an agent outside AQuA's project, that "
        "project by id (6 to 30 lowercase letters, digits and hyphens). Give "
        "the project by id with --observed-project."
    )


def _validate_merged_key(
    stored: store.AgentRecord, after: store.AgentRecord
) -> str:
    """Checks that a merge keeps an attachment's identity.

    Args:
        stored: How the agent is attached now.
        after: The merged record.

    Returns:
        The reason the merge is refused, or an empty string if it is not.
    """
    key = stored.agent_key
    before_project = keys.resolve_key_project(
        stored.config.observed_project_id, env_config
    )
    after_project = keys.resolve_key_project(
        after.config.observed_project_id, env_config
    )
    if before_project != after_project:
        return (
            f"{key} keeps its project, {before_project}; observed_project_id "
            f"{after_project!r} would make it another agent. Attach that one "
            "without the key."
        )
    if (
        stored.observed_engine_id
        and after.observed_engine_id
        and stored.observed_engine_id != after.observed_engine_id
    ):
        return (
            f"{key} is attached for engine {stored.observed_engine_id}; "
            f"refusing to re-point it at {after.observed_engine_id}. Detach it "
            "first to attach the other engine under this key."
        )
    return ""


def build_trigger_target() -> dict[str, str]:
    """Builds what an attached agent's triggers call: this engine, as it knows itself.

    `attach` wires the update trigger to the topic; the rest names the
    instance.

    Returns:
        The engine's region, resource name, topic and caller account, or an
        empty dictionary when one is unknown, as in a local run, which has no
        topic and no engine id.
    """
    cfg = env_config
    if not (cfg.ambient_topic and cfg.ambient_caller_sa and cfg.aqa_engine_id):
        return {}
    return {
        "region": cfg.location,
        "aqua_engine": (
            f"projects/{cfg.project_id}/locations/{cfg.location}"
            f"/reasoningEngines/{cfg.aqa_engine_id}"
        ),
        "ambient_topic": cfg.ambient_topic,
        "ambient_caller_email": cfg.ambient_caller_sa,
    }


async def record_applied(
    *,
    agent_key: str,
    applied: dict[str, Any] | None = None,
    granted: list[Any] | None = None,
    revoked: list[Any] | None = None,
) -> dict[str, Any]:
    """Records what `attach` wired and granted, and what `detach` revoked.

    The trigger values are the ones the CLI applied, not the ones stored now:
    another attach may have landed in between, and the object must not claim
    that one is in effect.

    Grants are merged into the stored list rather than sent whole, so a call
    that records one grant leaves the others in place. Only grants to this
    engine's own service account are accepted: `detach` runs the
    revocations with an operator's credentials, and must not be pointed at
    anybody else's binding.

    Args:
        agent_key: Key of the attached agent.
        applied: Every field the update trigger acts on, as wired; None when
            no trigger was wired.
        granted: Grants just made, each `{resource, role, member}`; they are
            added to the recorded ones.
        revoked: Grants just revoked, in the same shape; they are removed from
            the recorded ones.

    Returns:
        The agent, the fields still not in effect and the grants now recorded,
        or `{"error": ...}` when the request is refused, the agent is not
        attached, or storage fails.
    """
    name = agent_key.strip()
    if refusal := _validate_applied(name, applied, granted, revoked):
        return {"error": refusal}
    if granted:
        account = await asyncio.to_thread(access_check.resolve_service_account)
        member = f"serviceAccount:{account}"
        if not account or any(g["member"] != member for g in granted):
            return {
                "error": "Only grants to this deployment's service account "
                f"({account or 'unknown'}) can be recorded."
            }
    jobs = objects.jobs_store_factory()
    try:
        stored = await asyncio.to_thread(store.get_agent_record, jobs, name)
        if stored is None:
            return {
                "error": f"{name} is not attached, so there is nothing to record "
                "an apply on."
            }
        before = stored.applied or store.Applied()
        grants = _merge_grants(
            before.grants,
            granted=[store.AppliedGrant(**g) for g in granted or []],
            revoked=[store.AppliedGrant(**g) for g in revoked or []],
        )
        if applied is not None:
            after = store.Applied(
                at=dt.datetime.now(dt.UTC).isoformat(),
                grants=grants,
                **applied,
            )
        else:
            after = dataclasses.replace(before, grants=grants)
        record = dataclasses.replace(stored, applied=after)
        saved = await asyncio.to_thread(store.save_agent_record, jobs, record)
    except objects.StorageNotConfiguredError:
        return {"error": _NO_BUCKET}
    except Exception as exc:
        logger.warning(
            "attach: recording the apply for %s failed", name, exc_info=True
        )
        return {"error": f"Could not record the apply for {name}: {exc}"}
    effective_config.forget_cached_record(name)
    return {
        "agent_key": name,
        "agent_name": saved.agent_name,
        "applied_at": saved.applied.at if saved.applied else "",
        "unapplied_fields": saved.list_unapplied_fields(),
        "grants": _list_grant_records(saved),
    }


def _merge_grants(
    recorded: tuple[store.AppliedGrant, ...],
    *,
    granted: list[store.AppliedGrant],
    revoked: list[store.AppliedGrant],
) -> tuple[store.AppliedGrant, ...]:
    """Merges grants just made and just revoked into the recorded ones.

    Args:
        recorded: Grants recorded so far.
        granted: Grants to add; one already recorded is kept once.
        revoked: Grants to remove.

    Returns:
        The grants to record, earliest first.
    """
    gone = {g.key for g in revoked}
    merged: dict[tuple[str, str, str], store.AppliedGrant] = {}
    for grant in (*recorded, *granted):
        if grant.key not in gone:
            merged.setdefault(grant.key, grant)
    return tuple(merged.values())


def _list_grant_records(record: store.AgentRecord) -> list[dict[str, str]]:
    """Lists the grants recorded for an agent, as the routes report them.

    Args:
        record: Agent record.

    Returns:
        One `{resource, role, member}` per recorded grant.
    """
    if record.applied is None:
        return []
    return [dataclasses.asdict(g) for g in record.applied.grants]


async def list_agents() -> dict[str, Any]:
    """Lists every attached agent, and which one answers a request that names none.

    An object that cannot be read is listed with the reason, so a broken
    attachment is not mistaken for no attachment.

    Returns:
        Where the agents are kept, one entry per attached agent, the default
        agent's key, and this engine's trigger target, or `{"error": ...}`
        when listing fails.
    """
    jobs = objects.jobs_store_factory()
    try:
        names = await asyncio.to_thread(store.list_agents, jobs)
    except objects.StorageNotConfiguredError:
        return {"error": _NO_BUCKET}
    except Exception as exc:
        logger.warning("attach: listing attached agents failed", exc_info=True)
        return {"error": f"Could not list the attached agents: {exc}"}

    agents = []
    flagged = []
    for name in names:
        entry: dict[str, Any] = {"agent_key": name}
        try:
            record = await asyncio.to_thread(store.get_agent_record, jobs, name)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            agents.append(entry)
            continue
        if record is None:
            # Deleted between the listing and the read.
            continue
        entry.update(
            agent_name=record.agent_name,
            default=record.default,
            observed_deployment_name=record.observed_deployment_name,
            # What `publish-source` finds the attachment of an engine by.
            observed_engine_id=record.observed_engine_id,
            # Where `detach` finds the agent's update sink.
            observed_project_id=record.config.observed_project_id,
            # When the agent is investigated, read by the engine on every tick.
            investigation_schedule=record.investigation_schedule,
            scheduled_trigger_enabled=record.scheduled_trigger_enabled,
            updated_at=record.updated_at,
            unapplied_fields=record.list_unapplied_fields(),
            # What `detach` revokes.
            grants=_list_grant_records(record),
        )
        if record.default:
            flagged.append(name)
        agents.append(entry)

    # The rule `store.resolve_default` applies, computed over the records
    # already read here so that each object is read once.
    candidates = flagged or [a["agent_key"] for a in agents if "error" not in a]
    default_agent = candidates[0] if candidates else None
    # The agent a request that selects none gets, which `publish-source`
    # files an engine's source under when no attachment records the engine,
    # unless the default records another engine.
    for entry in agents:
        entry["watched"] = entry["agent_key"] == default_agent
    return {
        "prefix": jobs.uri(store.AGENTS_PREFIX),
        "agents": agents,
        "default_agent": default_agent,
        "trigger_target": build_trigger_target(),
    }


async def detach_agent(*, agent_key: str) -> dict[str, Any]:
    """Deletes an agent's stored object.

    Args:
        agent_key: Key of the agent to detach.

    Returns:
        Whether an object was deleted, or `{"error": ...}` when the key is
        refused or deletion fails.
    """
    name = agent_key.strip()
    if refusal := _validate_agent_name(name):
        return {"error": refusal}
    jobs = objects.jobs_store_factory()
    try:
        deleted = await asyncio.to_thread(store.delete_agent_record, jobs, name)
    except objects.StorageNotConfiguredError:
        return {"error": _NO_BUCKET}
    except Exception as exc:
        logger.warning(
            "attach: deleting the object for %s failed", name, exc_info=True
        )
        return {"error": f"Could not detach {name}: {exc}"}
    effective_config.forget_cached_record(name)
    return {"agent_key": name, "detached": deleted}


def _validate_agent_name(name: str) -> str:
    """Checks that an agent name or key maps to its own object key.

    Args:
        name: Agent name or key to check.

    Returns:
        The reason the name is refused, or an empty string if it is not.
    """
    if "/" not in name:
        return ""
    return (
        f"Agent {name!r} contains '/'. The object key would substitute it, "
        "attaching the agent under a name nobody asked for."
    )


def _validate_command(name: str, fields: dict[str, Any]) -> str:
    """Checks a request before anything is read.

    Args:
        name: Resolved agent name, or the key of the attachment to change.
        fields: Requested fields, keyed by field name.

    Returns:
        The reason the request is refused, or an empty string if it is not.
    """
    if not name:
        return "Which agent? Name the agent to attach."
    if refusal := _validate_agent_name(name):
        return refusal
    unknown = sorted(set(fields) - SETTABLE_FIELDS)
    if unknown:
        return (
            f"Not fields of an attachment: {', '.join(unknown)}. "
            f"Settable: {', '.join(sorted(SETTABLE_FIELDS))}."
        )
    annotations = {**_CONFIG_FIELDS, **_RECORD_FIELDS}
    wrong = []
    for field, value in sorted(fields.items()):
        if not _matches_annotation(value, annotations[field]):
            wrong.append(f"{field} must be {annotations[field]}, got {value!r}")
        elif field in _KNOWN_METRICS:
            known = _KNOWN_METRICS[field]
            if unknown := [m for m in value if m not in known]:
                wrong.append(
                    f"Unknown metric in {field}: {', '.join(unknown)}. "
                    f"Accepted prebuilt metrics for eval_service mode: "
                    f"{', '.join(sorted(known))}. Published code metrics are "
                    "scored by session review without attach; run "
                    "`agents-cli aqua metrics list` to see what is published"
                )
    cron = fields.get("investigation_schedule")
    if isinstance(cron, str) and not schedule.is_valid_schedule(cron):
        # Stored, it would fail every tick, and the agent would never be
        # investigated on a schedule.
        wrong.append(
            "investigation_schedule must be a five-field cron expression in "
            f"UTC that names a real date, got {cron!r}; the default is "
            f"{store.DEFAULT_INVESTIGATION_SCHEDULE!r}"
        )
    bucket = fields.get("telemetry_payload_bucket")
    if (
        isinstance(bucket, str)
        and bucket
        and not _BUCKET_NAME_RE.fullmatch(bucket)
    ):
        wrong.append(
            "telemetry_payload_bucket must be a bucket name, without gs:// or a "
            f"path, got {bucket!r}"
        )
    return "; ".join(wrong)


def _validate_applied(
    name: str,
    applied: dict[str, Any] | None,
    granted: list[Any] | None,
    revoked: list[Any] | None,
) -> str:
    """Checks a `record_applied` request before anything is read.

    Args:
        name: Agent name.
        applied: Requested applied values, keyed by field name, or None.
        granted: Grants to add, or None.
        revoked: Grants to remove, or None.

    Returns:
        The reason the request is refused, or an empty string if it is not.
    """
    if not name:
        return "Which agent? agent_key is empty."
    if refusal := _validate_agent_name(name):
        return refusal
    if applied is None and granted is None and revoked is None:
        return "Nothing to record: give applied, granted or revoked."
    for label, grants in (("granted", granted), ("revoked", revoked)):
        for grant in grants or []:
            if not store.is_grant_record(grant):
                return (
                    f"{label} entries must name exactly "
                    f"{', '.join(sorted(store.GRANT_FIELDS))} as non-empty "
                    f"strings; got {grant!r}."
                )
    if applied is None:
        return ""
    expected = set(store.TRIGGER_FIELDS)
    if set(applied) != expected:
        return (
            f"applied must name exactly {', '.join(sorted(expected))}; "
            f"got {', '.join(sorted(applied)) or 'none'}."
        )
    wrong = [
        f"{field} must be {_APPLIED_FIELDS[field]}, got {value!r}"
        for field, value in sorted(applied.items())
        if not _matches_annotation(value, _APPLIED_FIELDS[field])
    ]
    return "; ".join(wrong)


def _matches_annotation(value: Any, annotation: Any) -> bool:
    """Checks that a value has the shape a field's annotation declares.

    The runtime's validation assumes the types are right: it clamps an int and
    compares a string, and a string where a list belongs would be iterated one
    character at a time.

    Args:
        value: Requested value.
        annotation: The field's annotation, as a string.

    Returns:
        True if the value conforms, or if the annotation is one this check does
        not cover and the runtime's own validation decides.
    """
    if annotation == "str":
        return isinstance(value, str)
    if annotation == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation == "bool":
        return isinstance(value, bool)
    if annotation == "bool | None":
        return value is None or isinstance(value, bool)
    if annotation == "list[str]":
        return isinstance(value, list) and all(
            isinstance(v, str) for v in value
        )
    return True


def _is_default_once_saved(
    jobs: objects.ObjectStore, record: store.AgentRecord
) -> bool:
    """Checks whether `record`'s agent is the default once it is saved.

    The default is the agent a request that selects none gets: the default
    attached agent, which saving `record` can change.

    Args:
        jobs: Store holding the attached agents.
        record: The agent's record as it would be saved.

    Returns:
        Whether the agent is the default. False when the other attachments
        cannot be read, since a request selecting none then gets no agent.
    """
    try:
        return store.resolve_default(jobs, pending=record) == record.agent_key
    except Exception:
        logger.warning(
            "attach: resolving the default attached agent failed", exc_info=True
        )
        return False


def _build_unattached_record(name: str, key: str) -> store.AgentRecord:
    """Builds the record describing how an agent is observed with nothing stored.

    Args:
        name: Agent name.
        key: The key it is attached under.

    Returns:
        The defaults, under the agent's name and key.
    """
    return store.AgentRecord(
        config=ObservedAgentConfig(observed_agent_name=name, agent_key=key)
    )


def _merge_fields(
    before: store.AgentRecord, fields: dict[str, Any]
) -> store.AgentRecord:
    """Applies requested fields to a record and validates them as the runtime does.

    Args:
        before: How the agent is observed now.
        fields: Requested fields, keyed by field name.

    Returns:
        The merged record, with runtime fields as the runtime will use them.

    Raises:
        ValueError: If the runtime refuses a value outright.
    """
    requested = dataclasses.replace(
        before.config,
        **{k: v for k, v in fields.items() if k in _CONFIG_FIELDS},
    )
    validated = config_module.extract_agent_config(
        env_config.copy_with_agent(requested)
    )
    return dataclasses.replace(
        before,
        config=validated,
        **{k: v for k, v in fields.items() if k in _RECORD_FIELDS},
    )


def _extract_settable_values(record: store.AgentRecord) -> dict[str, Any]:
    """Extracts every settable field of a record as a flat dictionary.

    Args:
        record: Agent record.

    Returns:
        Settable field values keyed by field name, as the object stores them.
    """
    values = dataclasses.asdict(record.config)
    values.update({name: getattr(record, name) for name in _RECORD_FIELDS})
    return {k: v for k, v in values.items() if k in SETTABLE_FIELDS}


def _compute_changes(
    before: store.AgentRecord, after: store.AgentRecord
) -> list[dict[str, Any]]:
    """Computes the settable fields whose value differs between two records.

    Args:
        before: How the agent is observed now.
        after: How it will be observed once stored.

    Returns:
        One `{field, before, after}` entry per changed field, ordered by name.
    """
    old = _extract_settable_values(before)
    new = _extract_settable_values(after)
    return [
        {"field": field, "before": old[field], "after": new[field]}
        for field in sorted(SETTABLE_FIELDS)
        if old[field] != new[field]
    ]


def _compute_adjustments(
    fields: dict[str, Any], after: store.AgentRecord
) -> list[dict[str, Any]]:
    """Computes the requested values the runtime changed on the way in.

    The runtime clamps some numbers into range and drops metrics it does not
    know; the caller is told rather than left to find out from a later read.

    Args:
        fields: Requested fields, keyed by field name.
        after: The merged, validated record.

    Returns:
        One `{field, requested, stored}` entry per adjusted field, ordered by name.
    """
    stored = _extract_settable_values(after)
    return [
        {"field": field, "requested": value, "stored": stored[field]}
        for field, value in sorted(fields.items())
        if stored[field] != value
    ]
