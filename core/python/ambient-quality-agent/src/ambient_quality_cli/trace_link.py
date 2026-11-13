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

"""A project's Cloud Trace spans, and the BigQuery link `cloud_ops` reads.

Cloud Trace stores a project's spans in its observability bucket `_Trace`, in
the dataset `Spans`. `cloud_ops` ingestion reads them through a BigQuery link
on that dataset, whose id names the linked BigQuery dataset. `attach` and
`link-traces` look both up here, and create what is missing: lookups run on
the user's ADC over REST, creation runs gcloud with the user's credentials::

    resolve_trace_link
      ├── GET  observability  locations/-/buckets          the _Trace bucket
      ├── POST cloudresourcemanager  :testIamPermissions   with the API off
      ├── GET  observability  locations/global/settings    with no bucket
      ├── GET  observability  .../datasets/Spans/links
      └── GET  bigquery  datasets/<link>                   location
    create_trace_link
      ├── gcloud services enable, then poll the buckets    with the API off
      ├── gcloud observability buckets create
      ├── gcloud observability buckets datasets links create
      └── poll GET bigquery datasets/<link>                until it is readable
"""

from __future__ import annotations

import dataclasses
import re
import shlex
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from ambient_quality_cli.rest_client import ApiError, JsonApi

TRACE_BUCKET = "_Trace"
TRACE_DATASET = "Spans"
SPANS_VIEW = "_AllSpans"
DEFAULT_TRACE_LINK = "trace_spans_linked"
DEFAULT_BUCKET_LOCATION = "us"
_TRACE_BUCKET_RE = re.compile(
    rf"^projects/[^/]+/locations/(?P<location>[^/]+)/buckets/{TRACE_BUCKET}$"
)

_SERVICE = "observability.googleapis.com"
_ENABLE_PERMISSION = "serviceusage.services.enable"

_BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2"
_OBSERVABILITY = "https://observability.googleapis.com/v1"
_RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v3"

# A newly enabled API can return SERVICE_DISABLED for several minutes, and
# BigQuery can take several minutes to surface a new linked dataset (~4 minutes total).
_POLL_DELAYS_SECONDS = (0, 5, 10, 15, 30, 30, 60, 60)

_CREATE_REQUIREMENTS = (
    "Creating them needs roles/observability.editor and bigquery.datasets.get "
    "(roles/bigquery.metadataViewer holds it) on the project, and gcloud "
    "586.0.0 or later."
)

_T = TypeVar("_T")


class TraceLinkError(Exception):
    """The trace bucket or its link cannot be looked up or created."""


@dataclasses.dataclass(frozen=True)
class TraceLink:
    """The BigQuery link on a project's Cloud Trace spans."""

    project: str
    """The project, by id."""

    location: str
    """The location of its `_Trace` bucket, such as `us`."""

    link_id: str
    """The link's id, which is also the linked BigQuery dataset's name."""

    exists: bool
    """Whether the link exists; if not, `attach` or `link-traces --apply` creates it."""

    bucket_exists: bool = True
    api_disabled: bool = False
    """Whether the Observability API was off, so the bucket and link are
    planned with default values."""
    can_enable_api: bool = False
    bigquery_location: str = dataclasses.field(default="", compare=False)
    notes: tuple[str, ...] = dataclasses.field(default=(), compare=False)

    @property
    def name(self) -> str:
        """The link's full resource name."""
        return (
            f"projects/{self.project}/locations/{self.location}"
            f"/buckets/{TRACE_BUCKET}/datasets/{TRACE_DATASET}"
            f"/links/{self.link_id}"
        )

    def build_create_argv(self) -> list[str]:
        """Builds the command that creates the link.

        gcloud waits for the creation to finish, so the linked dataset exists
        once the command returns.

        Returns:
            The `gcloud` argument vector.
        """
        return [
            "gcloud",
            "observability",
            "buckets",
            "datasets",
            "links",
            "create",
            self.name,
            f"--dataset={TRACE_DATASET}",
            f"--bucket={TRACE_BUCKET}",
            f"--location={self.location}",
            f"--project={self.project}",
        ]

    def build_needed_argvs(self) -> list[list[str]]:
        """Builds the gcloud commands still needed, in the order they run.

        Returns:
            Enabling the API, creating the bucket and creating the link, each
            only when needed.
        """
        project = f"--project={self.project}"
        argvs = []
        if self.api_disabled:
            argvs.append(["gcloud", "services", "enable", _SERVICE, project])
        if not self.bucket_exists:
            create = ["gcloud", "observability", "buckets", "create"]
            location = f"--location={self.location}"
            argvs.append([*create, TRACE_BUCKET, location, project])
        if not self.exists:
            argvs.append(self.build_create_argv())
        return argvs


def resolve_bigquery_location(bucket_location: str) -> str:
    """Resolves the BigQuery location of an observability bucket's link.

    Args:
        bucket_location: The bucket's location, such as `us` or `us-east1`.

    Returns:
        The same location, with BigQuery's capitals for `US` and `EU`.
    """
    if bucket_location in ("us", "eu"):
        return bucket_location.upper()
    return bucket_location


def resolve_trace_link(
    api: JsonApi, project: str, *, location: str | None = None
) -> TraceLink:
    """Looks up a project's `_Trace` bucket and link, planning missing resources.

    Plans a missing bucket at ``location``, the project's default storage
    location, or `DEFAULT_BUCKET_LOCATION` (in that order), and a missing link
    as `DEFAULT_TRACE_LINK`. When the Observability API is disabled, plans both
    with those defaults and checks whether the user has permission to enable the
    API.

    Args:
        api: Google REST client.
        project: GCP project ID.
        location: Bucket location to use when the bucket does not exist.

    Returns:
        The resolved or planned `TraceLink`.

    Raises:
        TraceLinkError: If a lookup fails for any reason other than the API
            being disabled.
    """
    try:
        bucket_location = _get_bucket_location(api, project)
    except ApiError as exc:
        if exc.reason != "SERVICE_DISABLED":
            raise TraceLinkError(
                f"Cannot list the observability buckets of {project} ({exc})."
            ) from exc
        return _build_disabled_api_link(api, project, location)

    if bucket_location is None:
        bucket_location = (
            location
            or _get_default_storage_location(api, project)
            or DEFAULT_BUCKET_LOCATION
        )
        return TraceLink(
            project,
            bucket_location,
            DEFAULT_TRACE_LINK,
            exists=False,
            bucket_exists=False,
            bigquery_location=resolve_bigquery_location(bucket_location),
        )

    links_url = (
        f"{_OBSERVABILITY}/projects/{project}/locations/{bucket_location}"
        f"/buckets/{TRACE_BUCKET}/datasets/{TRACE_DATASET}/links"
    )
    try:
        # Must specify the concrete bucket location; wildcard `locations/-`
        # returns an empty list of links.
        links = api.get_json(links_url).get("links") or []
    except ApiError as exc:
        raise TraceLinkError(
            f"Cannot list the BigQuery links of {TRACE_BUCKET}/{TRACE_DATASET} "
            f"in {project} ({exc})."
        ) from exc
    link_ids = [
        str(link["name"]).rsplit("/", 1)[-1]
        for link in links
        if isinstance(link, dict) and link.get("name")
    ]
    bigquery_location = resolve_bigquery_location(bucket_location)
    notes = []
    if link_ids:
        try:
            bigquery_location = (
                get_dataset_location(api, project, link_ids[0])
                or bigquery_location
            )
        except ApiError as exc:
            notes.append(
                f"Cannot read dataset {project}.{link_ids[0]} ({exc})."
            )
    return TraceLink(
        project,
        bucket_location,
        link_ids[0] if link_ids else DEFAULT_TRACE_LINK,
        exists=bool(link_ids),
        bigquery_location=bigquery_location,
        notes=tuple(notes),
    )


def get_dataset_location(
    api: JsonApi, project: str, dataset: str
) -> str | None:
    """Gets the BigQuery location of a dataset.

    Args:
        api: Google REST client.
        project: The dataset's project, by id.
        dataset: The dataset's name.

    Returns:
        The location, or None when the dataset reports none.

    Raises:
        ApiError: If the dataset cannot be read.
    """
    body = api.get_json(f"{_BIGQUERY}/projects/{project}/datasets/{dataset}")
    return str(body.get("location") or "") or None


def _get_bucket_location(api: JsonApi, project: str) -> str | None:
    """Gets the location of a project's `_Trace` bucket.

    Args:
        api: Google REST client.
        project: The project, by id.

    Returns:
        The location, or None when the project has no `_Trace`.

    Raises:
        ApiError: If the buckets cannot be listed.
    """
    body = api.get_json(
        f"{_OBSERVABILITY}/projects/{project}/locations/-/buckets"
    )
    for bucket in body.get("buckets") or []:
        if match := _TRACE_BUCKET_RE.match(str(bucket.get("name", ""))):
            return match["location"]
    return None


def _get_default_storage_location(api: JsonApi, project: str) -> str | None:
    """Gets where the project stores new observability data by default.

    Args:
        api: Google REST client.
        project: The project, by id.

    Returns:
        The location, or None when the project sets none or it cannot be read.
    """
    try:
        body = api.get_json(
            f"{_OBSERVABILITY}/projects/{project}/locations/global/settings"
        )
    except ApiError:
        return None
    return str(body.get("defaultStorageLocation") or "") or None


def _build_disabled_api_link(
    api: JsonApi, project: str, location: str | None
) -> TraceLink:
    """Plans the bucket and link of a project whose Observability API is off.

    Args:
        api: Google REST client, to check whether the user may enable the API.
        project: The project, by id.
        location: Where to plan the bucket.

    Returns:
        Both planned, with a note on whether the user may enable the API.
    """
    try:
        body = api.post_json(
            f"{_RESOURCE_MANAGER}/projects/{project}:testIamPermissions",
            {"permissions": [_ENABLE_PERMISSION]},
        )
        can_enable = _ENABLE_PERMISSION in (body.get("permissions") or [])
    except ApiError:
        can_enable = False
    if can_enable:
        note = (
            f"The Observability API is disabled in {project}. Applying enables "
            "it, then looks up the bucket and the link again."
        )
    else:
        note = (
            f"The Observability API is disabled in {project}, and you may not "
            f"enable it ({_ENABLE_PERMISSION}, which "
            "roles/serviceusage.serviceUsageAdmin holds). Someone who may can "
            f"run: gcloud services enable {_SERVICE} --project={project}"
        )
    bucket_location = location or DEFAULT_BUCKET_LOCATION
    return TraceLink(
        project,
        bucket_location,
        DEFAULT_TRACE_LINK,
        exists=False,
        bucket_exists=False,
        bigquery_location=resolve_bigquery_location(bucket_location),
        api_disabled=True,
        can_enable_api=can_enable,
        notes=(note,),
    )


def create_trace_link(
    link: TraceLink,
    *,
    api: JsonApi,
    run_command: Callable[[list[str]], int],
    echo: Callable[[str], None],
    location: str | None = None,
    link_id: str | None = None,
    sleep: Callable[[float], None] | None = None,
    delays: Sequence[float] = _POLL_DELAYS_SECONDS,
) -> tuple[TraceLink, list[list[str]]]:
    """Creates the missing resources planned by :func:`resolve_trace_link`.

    1. If the Observability API is disabled, enable it, wait until it responds,
       and re-run :func:`resolve_trace_link` to replace default plan values with
       actual project state.
    2. Create the `_Trace` bucket and BigQuery link if missing.
    3. Poll BigQuery until the newly linked dataset is readable, so subsequent
       Terraform and access checks succeed.

    Args:
        link: The `TraceLink` returned by :func:`resolve_trace_link`.
        api: Google REST client.
        run_command: Executes a command with the user's credentials and returns
            its exit status.
        echo: Prints a progress line.
        location: Bucket location override passed to :func:`resolve_trace_link`
            after enabling the API.
        link_id: Desired BigQuery link ID, or None to use the discovered or
            default ID.
        sleep: Sleep function; defaults to `time.sleep`.
        delays: Polling intervals in seconds.

    Returns:
        A tuple of the updated `TraceLink` and the list of executed commands.

    Raises:
        TraceLinkError: If the user lacks permission to enable the API, a
            command fails, an existing link has a different ID than ``link_id``,
            or polling times out.
    """
    sleep = sleep or time.sleep
    ran: list[list[str]] = []

    def run(argv: list[str]) -> None:
        """Runs one command, failing on a non-zero exit status.

        Args:
            argv: Command-line arguments vector.

        Raises:
            TraceLinkError: If the command fails.
        """
        echo(f"$ {shlex.join(argv)}")
        if status := run_command(argv):
            raise TraceLinkError(
                f"Exit status {status} from: {shlex.join(argv)}\n"
                f"{_CREATE_REQUIREMENTS}"
            )
        ran.append(argv)

    if link.api_disabled:
        if not link.can_enable_api:
            raise TraceLinkError(" ".join(link.notes))
        run(link.build_needed_argvs()[0])
        _poll(
            lambda: _is_api_answering(api, link.project),
            f"the Observability API in {link.project} to answer",
            echo=echo,
            sleep=sleep,
            delays=delays,
        )
        link = resolve_trace_link(api, link.project, location=location)
    if link_id and link_id != link.link_id:
        if link.exists:
            # An observability dataset takes only one link.
            raise TraceLinkError(
                f"{TRACE_BUCKET}/{TRACE_DATASET} in {link.project} is linked "
                f"to BigQuery as {link.link_id}, not {link_id}."
            )
        link = dataclasses.replace(link, link_id=link_id)
    # Cloud Trace routes a project's spans to the single `_Trace` bucket by name
    # without extra configuration. Although Cloud Trace auto-creates `_Trace` on
    # the first span (except for Cloud Run, Cloud Run functions, and App Engine),
    # creating the BigQuery link beforehand requires the bucket to exist first.
    for argv in link.build_needed_argvs():
        run(argv)
    bigquery_location = link.bigquery_location
    if not link.exists:
        project, dataset = link.project, link.link_id
        bigquery_location = _poll(
            lambda: _get_dataset_location_if_exists(api, project, dataset),
            f"BigQuery to show dataset {project}.{dataset}",
            echo=echo,
            sleep=sleep,
            delays=delays,
        )
    created = dataclasses.replace(
        link,
        bucket_exists=True,
        exists=True,
        bigquery_location=bigquery_location,
    )
    return created, ran


def _poll(
    check: Callable[[], _T | None],
    waiting_for: str,
    *,
    echo: Callable[[str], None],
    sleep: Callable[[float], None],
    delays: Sequence[float],
) -> _T:
    """Calls ``check`` after each delay until it returns a value.

    Args:
        check: Returns the awaited value, or None to keep waiting.
        waiting_for: What is awaited, for the progress lines.
        echo: Prints a progress line.
        sleep: Waits a number of seconds.
        delays: Seconds to wait before each call.

    Returns:
        The first value ``check`` returns.

    Raises:
        TraceLinkError: If ``check`` still returns None after the last delay.
    """
    for delay in delays:
        if delay:
            echo(f"Waiting {delay:g} s for {waiting_for}.")
            sleep(delay)
        if (value := check()) is not None:
            return value
    raise TraceLinkError(
        f"Gave up waiting for {waiting_for}; it can take several minutes. "
        "Run the command again."
    )


def _is_api_answering(api: JsonApi, project: str) -> bool | None:
    """Checks whether the Observability API stopped answering SERVICE_DISABLED.

    Args:
        api: Google REST client.
        project: The project, by id.

    Returns:
        True once it answers otherwise, a real error included; else None.
    """
    try:
        _get_bucket_location(api, project)
    except ApiError as exc:
        if exc.reason == "SERVICE_DISABLED":
            return None
    return True


def _get_dataset_location_if_exists(
    api: JsonApi, project: str, dataset: str
) -> str | None:
    """Checks whether BigQuery shows a dataset yet.

    Args:
        api: Google REST client.
        project: The project, by id.
        dataset: The dataset's name.

    Returns:
        Its location once it is found; None while it is not.

    Raises:
        TraceLinkError: If the dataset cannot be read for another reason,
            which waiting does not fix.
    """
    try:
        return get_dataset_location(api, project, dataset)
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise TraceLinkError(
            f"Dataset {project}.{dataset} cannot be read ({exc}). "
            f"{_CREATE_REQUIREMENTS}"
        ) from exc


def format_trace_link_report(
    link: TraceLink, commands: list[list[str]], *, applied: bool
) -> dict[str, Any]:
    """Formats `link-traces`' JSON result.

    Args:
        link: The bucket and link as found, or as created.
        commands: The commands that ran, or that `--apply` runs.
        applied: Whether ``commands`` ran.

    Returns:
        The values the deployment root reads the spans with, and the commands.
    """
    return {
        "project": link.project,
        "link_id": link.link_id,
        "telemetry_table": SPANS_VIEW,
        "bigquery_location": link.bigquery_location,
        "applied": applied,
        "commands": [shlex.join(argv) for argv in commands],
    }
