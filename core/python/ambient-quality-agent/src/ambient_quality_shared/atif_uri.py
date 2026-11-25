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

"""Parses an `atif_uri`: a Cloud Storage prefix or a local directory.

An `atif_uri` is one string with two meanings. `parse_atif_uri` is the only
place that decides which, and it returns the parts, so no caller splits the
string again:

    "gs://acme-runs/billing"  ->  GcsPrefix(bucket="acme-runs", prefix="billing")
    "gs://acme-runs"          ->  GcsPrefix(bucket="acme-runs", prefix="")
    "/home/me/runs"           ->  LocalPath(path="/home/me/runs")
    "s3://acme-runs/x"        ->  ValueError
    "gs:/acme-runs/x"         ->  ValueError

A value that starts with a URL scheme must be `gs://`; anything else is a
local path. A value that is unclear is refused, so a URL of another scheme is
never read as a path. The string is split by hand: `urllib.parse.urlsplit`
drops newlines, strips leading spaces and cuts at `?`, so its parts can differ
from what was written.

The module imports only the standard library, so the agent and the vendored
CLI can share it.
"""

from __future__ import annotations

import dataclasses
import re

_GCS_SCHEME = "gs://"
"""How an `atif_uri` in Cloud Storage starts."""

_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
"""A URL scheme at the start of a value, such as `s3:`, `https:` or `GS:`."""

_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
"""The characters of a Cloud Storage bucket name: lowercase letters, digits,
dots, dashes and underscores, starting and ending with a letter or digit.
Cloud Storage decides whether the bucket exists; this check restricts names
that may be printed in a command to these characters."""


@dataclasses.dataclass(frozen=True)
class GcsPrefix:
    """A prefix in a Cloud Storage bucket.

    Attributes:
        bucket: The bucket name.
        prefix: The prefix inside the bucket, without a leading or trailing
            `/`; empty for the whole bucket.
    """

    bucket: str
    prefix: str


@dataclasses.dataclass(frozen=True)
class LocalPath:
    """A local directory.

    Attributes:
        path: The path as written; it may be relative.
    """

    path: str


def parse_atif_uri(uri: str) -> GcsPrefix | LocalPath:
    """Parses an `atif_uri`, reading nothing.

    Args:
        uri: The value, as `gs://<bucket>[/<prefix>]` or a local directory.

    Returns:
        The bucket and prefix, or the local path.

    Raises:
        ValueError: When the value is empty, holds a control character, has a
            scheme other than `gs://`, names no valid bucket, holds `?` or
            `#`, or has an empty, `.` or `..` segment in the prefix. The
            message is "atif_uri '<uri>' is not valid: <reason>".
    """
    if violation := _find_violation(uri):
        raise ValueError(f"atif_uri {uri!r} is not valid: {violation}")
    if not uri.startswith(_GCS_SCHEME):
        return LocalPath(uri)
    bucket, _, prefix = uri[len(_GCS_SCHEME) :].partition("/")
    return GcsPrefix(bucket, prefix.removesuffix("/"))


def _find_violation(uri: str) -> str | None:
    """Explains why an `atif_uri` is refused.

    Args:
        uri: The value.

    Returns:
        The reason; None when the value is a well-formed `gs://` prefix or a
        local path.
    """
    if not uri.strip():
        return "expected a gs:// prefix or a local directory"
    if any(ch < " " or ch == "\x7f" for ch in uri):
        return "it cannot hold a control character"
    if not _SCHEME_RE.match(uri):
        return None
    if not uri.startswith(_GCS_SCHEME):
        if uri.lower().startswith("gs:"):
            return "write a Cloud Storage prefix as gs://<bucket>/<prefix>"
        return "use a gs:// prefix or a local directory"
    rest = uri[len(_GCS_SCHEME) :]
    if "?" in rest or "#" in rest:
        return "a gs:// prefix cannot hold '?' or '#'"
    bucket, _, prefix = rest.partition("/")
    if not _BUCKET_RE.fullmatch(bucket):
        return f"{bucket!r} is not a Cloud Storage bucket name"
    # One trailing `/` is allowed; any other empty segment is refused.
    segments = prefix.split("/")
    if segments[-1] == "":
        segments.pop()
    if any(segment in ("", ".", "..") for segment in segments):
        return "a gs:// prefix cannot have empty, '.' or '..' segments"
    return None
