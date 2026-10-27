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

"""Unit tests for snapshot object keys, manifest validation, ignore patterns, and revision ordering."""

from __future__ import annotations

import json

import pytest
from ambient_quality_agent.tools.source_code import snapshot

MANIFEST = {
    "schema_version": 1,
    "revision": "10",
    "engine": "projects/p/locations/us-east1/reasoningEngines/1846",
    "created_at": "2026-09-11T13:43:00Z",
    "agent_directory": "app",
    "ignore_patterns": [".git", "terraform/", "/tests/"],
    "files": [{"path": "app/agent.py", "size": 2413, "truncated": False}],
    "total_bytes": 123456,
    "truncated_files": 0,
    "omitted_files": 0,
}


def test_object_keys_follow_the_published_layout() -> None:
    assert snapshot.build_manifest_object_name("10") == "10/manifest.json"
    assert (
        snapshot.build_file_object_name("10", "app/agent.py")
        == "10/files/app/agent.py"
    )
    assert snapshot.build_revision_prefix("10") == "10/"


def test_file_object_key_normalizes_a_leading_slash() -> None:
    assert (
        snapshot.build_file_object_name("2", "/app/agent.py")
        == "2/files/app/agent.py"
    )


def test_caps_match_the_published_contract() -> None:
    assert snapshot.MAX_FILE_BYTES == 1024 * 1024
    assert snapshot.MAX_SNAPSHOT_BYTES == 50 * 1024 * 1024


def test_parse_manifest_round_trips_the_documented_document() -> None:
    manifest = snapshot.parse_manifest(json.dumps(MANIFEST))

    assert manifest.revision == "10"
    assert manifest.agent_directory == "app"
    assert manifest.total_bytes == 123456
    assert [f.path for f in manifest.files] == ["app/agent.py"]
    assert manifest.get_entry("app/agent.py") is not None
    assert manifest.get_entry("app/missing.py") is None


def test_parse_manifest_rejects_a_non_json_body() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        snapshot.parse_manifest("not json at all")


def test_parse_manifest_rejects_a_malformed_file_entry() -> None:
    broken = {**MANIFEST, "files": [{"size": "huge"}]}

    with pytest.raises(ValueError, match="snapshot schema"):
        snapshot.parse_manifest(json.dumps(broken))


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        (".git/config", ".git"),
        ("terraform/main.tf", "terraform/"),
        ("modules/terraform/main.tf", "terraform/"),
        ("tests/test_agent.py", "/tests/"),
        ("build/out.pyc", "*.pyc"),
        (
            "terraform/main.tf",
            "terraform/*.tf",
        ),  # Single wildcard matches within a single segment.
        (
            "terraform/modules/aqa/main.tf",
            "terraform/**/*.tf",
        ),  # Recursive wildcard matches across directory boundaries.
        ("app/tools/gcs.py", "**/tools/"),
        ("app/generated/schema.py", "app/**"),
        ("app/agent.pyc", "*.py?"),
        ("logs/debug.log", "/logs/"),
    ],
)
def test_ignore_pattern_matches(path: str, pattern: str) -> None:
    assert snapshot.matches_ignore_pattern(path, pattern)


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        (
            "app/tests/test_agent.py",
            "/tests/",
        ),  # Leading slash anchors the pattern to the repository root.
        ("app/agent.py", "terraform/"),
        ("gitignore", ".git"),
        (
            "terraform",
            "terraform/",
        ),  # Directory patterns only match paths with descendants.
        ("app/agent.py", ""),
        ("app/agent.py", "# a comment"),
        (
            "terraform/modules/aqa/main.tf",
            "terraform/*.tf",
        ),  # Single wildcard does not cross directory boundaries.
        ("app/deep/tools/x.py", "app/*/x.py"),
        ("app/agent.py", "   "),
        (
            "app/agent.py",
            "# What a deploy must not upload. Both */ and **/ appear below.",
        ),  # Ignore-file comments preserved in the manifest match nothing.
    ],
)
def test_ignore_pattern_does_not_match(path: str, pattern: str) -> None:
    assert not snapshot.matches_ignore_pattern(path, pattern)


def test_excluding_pattern_names_the_rule_that_hid_a_path() -> None:
    manifest = snapshot.parse_manifest(json.dumps(MANIFEST))

    assert manifest.find_excluding_pattern("terraform/main.tf") == "terraform/"
    assert manifest.find_excluding_pattern("app/other.py") is None


def test_revisions_sort_numerically_and_are_not_assumed_contiguous() -> None:
    # Revision numbers may be non-contiguous due to deletions.
    assert snapshot.sort_revisions_desc(["1", "10", "2", "8"]) == [
        "10",
        "8",
        "2",
        "1",
    ]


def test_non_numeric_revision_is_ordered_rather_than_crashing() -> None:
    ordered = snapshot.sort_revisions_desc(["8", "draft", "10"])

    assert ordered == ["10", "8", "draft"]


def test_a_digit_like_key_that_is_not_an_integer_does_not_crash_the_sort() -> (
    None
):
    # Non-ASCII digits that cannot be parsed as decimal integers must not crash sorting.
    assert snapshot.sort_revisions_desc(["10", "\u00b2"]) == ["10", "\u00b2"]


def test_parse_manifest_refuses_a_newer_schema_version() -> None:
    future = {**MANIFEST, "schema_version": snapshot.SCHEMA_VERSION + 1}

    with pytest.raises(ValueError, match="newer than"):
        snapshot.parse_manifest(json.dumps(future))
