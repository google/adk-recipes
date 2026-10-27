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

"""The grant commands that let AQuA read what an attach points it at.

An attach asks the deployment to try every read an investigation makes, as
AQuA's own service account. Each read it reports as denied maps to one command
that grants the missing access:

| Denied check     | Command                                                |
|------------------|--------------------------------------------------------|
| `table`, `rows`  | `bq query` running `GRANT roles/bigquery.dataViewer    |
|                  | ON SCHEMA`: the dataset access entry Terraform's       |
|                  | `telemetry_reader` makes                               |
| `payload`        | `gcloud storage buckets add-iam-policy-binding` with   |
|                  | `roles/storage.objectViewer`                           |
| `log_view`       | `gcloud projects add-iam-policy-binding` with          |
|                  | `roles/logging.viewAccessor`                           |

The dataset grant is a `GRANT` statement because dataset-level IAM
(`bq add-iam-policy-binding --dataset`) needs allowlisting, and editing the
access list with `bq update --source` replaces the whole list. `GRANT` adds
one entry in a single call, and granting twice leaves one entry.

Every command is idempotent, so running one whose access already exists, or
running them again after a partial failure, changes nothing.

Each grant that ran is recorded on the attachment, and `detach` builds
the matching revoke command from that record alone: `REVOKE ... ON SCHEMA ...
FROM` for a dataset, and `remove-iam-policy-binding` for a bucket or a project.
A binding the record does not name is never revoked. A grant that found its
binding already in place is recorded like any other: it may have been left by
an earlier `attach` that failed or was interrupted, and `detach` revokes
everything `attach` granted.

This module only builds the commands, so code that shows them without running
them can import it; `ambient_quality_cli.grants` runs them with the user's
credentials.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

DENIED = "denied"
"""The access check status that calls for a grant."""

_DATASET_ROLE = "roles/bigquery.dataViewer"
_BUCKET_ROLE = "roles/storage.objectViewer"
_LOG_VIEW_ROLE = "roles/logging.viewAccessor"

# The values substituted into a command. A target the engine reports outside
# these shapes, matched in full, gets no command, so nothing unexpected reaches
# the printed line or the SQL statement. A project is an id, which may carry a
# domain prefix (`example.com:project`), or a number: on Agent Runtime the
# engine's `GOOGLE_CLOUD_PROJECT`, which its targets are built from, is the
# project number.
_PROJECT_RE = re.compile(r"(?:[a-z][a-z0-9.-]*:)?[a-z][a-z0-9-]*|[0-9]+")
_DATASET_RE = re.compile(r"\w+", re.ASCII)
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+", re.ASCII)


@dataclasses.dataclass(frozen=True)
class _BindingCommand:
    """One command that adds or removes one IAM binding."""

    resource: str
    """The binding's resource: a dataset, a bucket or a project."""

    role: str
    """The binding's role."""

    member: str
    """The binding's member: `serviceAccount:<email>`."""

    argv: tuple[str, ...]
    """The command, as run."""

    def render(self) -> str:
        """Renders the command as one line to paste into a shell.

        Returns:
            The command with every argument shell-quoted.
        """
        return shlex.join(self.argv)

    def to_record(self) -> dict[str, str]:
        """Converts the binding to what the attachment records of it.

        Returns:
            The binding as `{resource, role, member}`, from which
            `build_revocations` rebuilds the command that removes it.
        """
        return {
            "resource": self.resource,
            "role": self.role,
            "member": self.member,
        }


@dataclasses.dataclass(frozen=True)
class Grant(_BindingCommand):
    """One command that grants AQuA's service account a read it was denied."""

    checks: tuple[str, ...]
    """The denied checks this command addresses."""

    @property
    def key(self) -> tuple[str, str]:
        """What the command grants: the resource and the role."""
        return (self.resource, self.role)


@dataclasses.dataclass(frozen=True)
class Revocation(_BindingCommand):
    """One command that removes a binding a recorded grant made."""


def build_grants(access: Mapping[str, Any]) -> tuple[list[Grant], list[str]]:
    """Builds the grant commands for every denied check.

    Checks that read the same resource share one command: a denied `table`
    and `rows` on one dataset need the same grant.

    Args:
        access: The plan's `access` section: `service_account` and `checks`.

    Returns:
        Tuple of (grants in check order, targets of the denied checks that got
        no command). A target is left without a command when the deployment
        named no service account, or the target is not one this module knows
        how to grant.
    """
    denied = [
        check
        for check in access.get("checks") or []
        if check.get("status") == DENIED
    ]
    account = str(access.get("service_account") or "")
    if not _EMAIL_RE.fullmatch(account):
        return [], [_format_check(check) for check in denied]

    grants: dict[tuple[str, str], Grant] = {}
    ungranted = []
    for check in denied:
        grant = _build_grant(check, account)
        if grant is None:
            ungranted.append(_format_check(check))
            continue
        if grant.key in grants:
            grant = dataclasses.replace(
                grants[grant.key],
                checks=(*grants[grant.key].checks, *grant.checks),
            )
        grants[grant.key] = grant
    return list(grants.values()), ungranted


def _build_grant(check: Mapping[str, Any], account: str) -> Grant | None:
    """Builds the command that addresses one denied check.

    Args:
        check: One access check result.
        account: AQuA's service account email.

    Returns:
        The grant, or None for a check this module has no command for.
    """
    name = str(check.get("name", ""))
    target = str(check.get("target", ""))
    member = f"serviceAccount:{account}"
    if name in ("table", "rows"):
        # `project.dataset.table`, where the project may itself contain dots.
        parts = target.rsplit(".", 2)
        if len(parts) != 3:
            return None
        project, dataset, _ = parts
        if not (
            _PROJECT_RE.fullmatch(project) and _DATASET_RE.fullmatch(dataset)
        ):
            return None
        # The job runs in the dataset's project, and BigQuery infers its
        # location from the dataset the statement names.
        statement = (
            f"GRANT `{_DATASET_ROLE}` ON SCHEMA `{project}.{dataset}` "
            f'TO "{member}"'
        )
        return Grant(
            checks=(name,),
            resource=f"{project}:{dataset}",
            role=_DATASET_ROLE,
            member=member,
            argv=(
                "bq",
                "query",
                "--nouse_legacy_sql",
                f"--project_id={project}",
                statement,
            ),
        )
    if name == "payload":
        parsed = urlparse(target)
        if parsed.scheme != "gs" or not _BUCKET_RE.fullmatch(parsed.netloc):
            return None
        bucket = f"gs://{parsed.netloc}"
        return Grant(
            checks=(name,),
            resource=bucket,
            role=_BUCKET_ROLE,
            member=member,
            argv=(
                "gcloud",
                "storage",
                "buckets",
                "add-iam-policy-binding",
                bucket,
                f"--member={member}",
                f"--role={_BUCKET_ROLE}",
            ),
        )
    if name == "log_view":
        project = target.removeprefix("projects/")
        if not target.startswith("projects/") or not _PROJECT_RE.fullmatch(
            project
        ):
            return None
        return Grant(
            checks=(name,),
            resource=target,
            role=_LOG_VIEW_ROLE,
            member=member,
            argv=(
                "gcloud",
                "projects",
                "add-iam-policy-binding",
                project,
                f"--member={member}",
                f"--role={_LOG_VIEW_ROLE}",
                # Without it, gcloud asks which condition to bind under when
                # the project's policy already has conditional bindings.
                "--condition=None",
            ),
        )
    return None


def build_revocations(
    records: Sequence[Any],
) -> tuple[list[Revocation], list[str]]:
    """Builds the command that removes each recorded grant.

    The record is validated against the same shapes a grant is built from, so
    a record that was tampered with reaches neither the command line nor the
    SQL statement.

    Args:
        records: Grants as the attachment recorded them, each
            `{resource, role, member}`.

    Returns:
        Tuple of (revocations in record order, recorded resources that got no
        command). A resource that is not printable text is named as an
        unreadable record, so nothing raw from the object reaches the terminal.
    """
    revocations = []
    unrevocable = []
    for record in records:
        if isinstance(record, Mapping) and (
            revocation := _build_revocation(record)
        ):
            revocations.append(revocation)
            continue
        resource = (
            record.get("resource") if isinstance(record, Mapping) else None
        )
        unrevocable.append(
            resource
            if isinstance(resource, str) and resource.isprintable() and resource
            else "an unreadable record"
        )
    return revocations, unrevocable


def _build_revocation(record: Mapping[str, Any]) -> Revocation | None:
    """Builds the command that removes one recorded grant.

    Args:
        record: The grant as recorded.

    Returns:
        The revocation, or None for a record outside the shapes a grant has.
    """
    resource = str(record.get("resource", ""))
    role = str(record.get("role", ""))
    member = str(record.get("member", ""))
    if not (
        member.startswith("serviceAccount:")
        and _EMAIL_RE.fullmatch(member.removeprefix("serviceAccount:"))
    ):
        return None
    if role == _DATASET_ROLE:
        # `project:dataset`, where the project may itself carry a domain.
        project, _, dataset = resource.rpartition(":")
        if not (
            _PROJECT_RE.fullmatch(project) and _DATASET_RE.fullmatch(dataset)
        ):
            return None
        statement = (
            f'REVOKE `{role}` ON SCHEMA `{project}.{dataset}` FROM "{member}"'
        )
        return Revocation(
            resource=resource,
            role=role,
            member=member,
            argv=(
                "bq",
                "query",
                "--nouse_legacy_sql",
                f"--project_id={project}",
                statement,
            ),
        )
    if role == _BUCKET_ROLE:
        bucket = resource.removeprefix("gs://")
        if not resource.startswith("gs://") or not _BUCKET_RE.fullmatch(bucket):
            return None
        return Revocation(
            resource=resource,
            role=role,
            member=member,
            argv=(
                "gcloud",
                "storage",
                "buckets",
                "remove-iam-policy-binding",
                resource,
                f"--member={member}",
                f"--role={role}",
            ),
        )
    if role == _LOG_VIEW_ROLE:
        project = resource.removeprefix("projects/")
        if not resource.startswith("projects/") or not _PROJECT_RE.fullmatch(
            project
        ):
            return None
        return Revocation(
            resource=resource,
            role=role,
            member=member,
            argv=(
                "gcloud",
                "projects",
                "remove-iam-policy-binding",
                project,
                f"--member={member}",
                f"--role={role}",
                # The grant bound it with no condition.
                "--condition=None",
            ),
        )
    return None


def _format_check(check: Mapping[str, Any]) -> str:
    """Formats a denied check for a line naming what got no command.

    Args:
        check: One access check result.

    Returns:
        The check's target, or its name when it reports no target.
    """
    return str(check.get("target") or check.get("name") or "")
