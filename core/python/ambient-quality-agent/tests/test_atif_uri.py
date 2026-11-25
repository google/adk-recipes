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

"""Tests for parsing an `atif_uri`."""

from __future__ import annotations

import pytest
from ambient_quality_shared.atif_uri import (
    GcsPrefix,
    LocalPath,
    parse_atif_uri,
)


@pytest.mark.parametrize(
    ("uri", "parsed"),
    [
        ("gs://acme-runs/billing", GcsPrefix("acme-runs", "billing")),
        ("gs://acme-runs/billing/", GcsPrefix("acme-runs", "billing")),
        ("gs://acme-runs/a/b", GcsPrefix("acme-runs", "a/b")),
        ("gs://acme-runs", GcsPrefix("acme-runs", "")),
        ("gs://acme-runs/", GcsPrefix("acme-runs", "")),
        ("gs://acme.runs/my runs", GcsPrefix("acme.runs", "my runs")),
        ("/home/me/runs", LocalPath("/home/me/runs")),
        ("runs/2026", LocalPath("runs/2026")),
        ("./a:b/c", LocalPath("./a:b/c")),
        ("/home/me/my runs", LocalPath("/home/me/my runs")),
    ],
)
def test_a_well_formed_atif_uri_is_parsed_into_its_parts(
    uri: str, parsed: GcsPrefix | LocalPath
) -> None:
    assert parse_atif_uri(uri) == parsed


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("", "expected a gs:// prefix"),
        ("   ", "expected a gs:// prefix"),
        ("GS://acme-runs/x", "write a Cloud Storage prefix as"),
        ("gs:/acme-runs/x", "write a Cloud Storage prefix as"),
        ("s3://acme-runs/x", "use a gs:// prefix or a local directory"),
        ("https://example.com/x", "use a gs:// prefix or a local directory"),
        ("a:b/c", "use a gs:// prefix or a local directory"),
        ("gs://", "is not a Cloud Storage bucket name"),
        ("gs:///x", "is not a Cloud Storage bucket name"),
        ("gs://Bad_Bucket/x", "is not a Cloud Storage bucket name"),
        ("gs://u@acme-runs/x", "is not a Cloud Storage bucket name"),
        ("gs://acme-runs:9/x", "is not a Cloud Storage bucket name"),
        ("gs://acme-runs/x?y=1", "cannot hold '?' or '#'"),
        ("gs://acme-runs/x#y", "cannot hold '?' or '#'"),
        ("gs://acme-runs/a//b", "empty, '.' or '..' segments"),
        ("gs://acme-runs/a//", "empty, '.' or '..' segments"),
        ("gs://acme-runs//", "empty, '.' or '..' segments"),
        ("gs://acme-runs//a", "empty, '.' or '..' segments"),
        ("gs://acme-runs/a/../b", "empty, '.' or '..' segments"),
        ("gs://acme-runs/./a", "empty, '.' or '..' segments"),
        ("/x/\x00y", "control character"),
        ("gs://acme-runs/a\nb", "control character"),
        ("\tgs://acme-runs/a", "control character"),
    ],
)
def test_a_malformed_atif_uri_is_refused_with_its_reason(
    uri: str, reason: str
) -> None:
    with pytest.raises(ValueError, match="is not valid") as caught:
        parse_atif_uri(uri)

    assert reason in str(caught.value)
