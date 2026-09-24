# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for recipe_manifests.scan().

close_orphan_dependabot_prs.py decides which open Dependabot PRs to close
(with --delete-branch) from this scan. A scan that under-reports directories
causes valid PRs to be destroyed, so the detectors and the walk are pinned
here rather than trusted.
"""

import json

import pytest
import recipe_manifests as m


def _touch(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def test_uv_detected_by_lockfile_not_pyproject(tmp_path):
    """pyproject.toml alone is not enough — Dependabot's uv ecosystem keys off
    the lockfile, and a recipe without one has nothing to update."""
    _touch(tmp_path / "pyproject.toml", "[project]\n")
    assert not m._is_uv(tmp_path)
    _touch(tmp_path / "uv.lock", "")
    assert m._is_uv(tmp_path)


def test_gradle_requires_settings_file_so_submodules_are_skipped(tmp_path):
    _touch(tmp_path / "build.gradle.kts", "")
    assert not m._is_gradle(tmp_path), "submodule must not be detected"
    _touch(tmp_path / "settings.gradle.kts", "")
    assert m._is_gradle(tmp_path), "root project must be detected"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"dependencies": {"react": "^18"}}, True),
        ({"devDependencies": {"vite": "^5"}}, True),
        # Config-only package.json (no deps) is not worth a Dependabot entry.
        ({"name": "x", "private": True}, False),
        ({}, False),
    ],
)
def test_npm_requires_actual_dependencies(tmp_path, payload, expected):
    _touch(tmp_path / "package.json", json.dumps(payload))
    assert m._is_npm(tmp_path) is expected


def test_npm_tolerates_malformed_package_json(tmp_path):
    """A broken manifest must not crash the scan and take the whole
    housekeeping run down with it."""
    _touch(tmp_path / "package.json", "{not json")
    assert m._is_npm(tmp_path) is False


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------


def test_scan_finds_all_roots_and_reports_repo_relative_paths(tmp_path):
    _touch(tmp_path / "core/python/alpha/uv.lock")
    _touch(tmp_path / "contrib/python/beta/uv.lock")
    _touch(tmp_path / "plugins/retail/gamma/uv.lock")
    assert m.scan(tmp_path) == [
        ("uv", "/contrib/python/beta"),
        ("uv", "/core/python/alpha"),
        ("uv", "/plugins/retail/gamma"),
    ]


def test_scan_reports_nested_manifests_separately(tmp_path):
    """One recipe can own several manifests at different depths — this is
    exactly what a fixed-depth glob would miss."""
    _touch(tmp_path / "core/python/rag/uv.lock")
    _touch(tmp_path / "core/python/rag/data_ingestion/uv.lock")
    _touch(
        tmp_path / "core/python/rag/web/server/package.json",
        json.dumps({"dependencies": {"express": "^4"}}),
    )
    assert m.scan(tmp_path) == [
        ("npm", "/core/python/rag/web/server"),
        ("uv", "/core/python/rag"),
        ("uv", "/core/python/rag/data_ingestion"),
    ]


def test_scan_reports_multiple_ecosystems_in_one_directory(tmp_path):
    _touch(tmp_path / "core/python/poly/uv.lock")
    _touch(
        tmp_path / "core/python/poly/package.json",
        json.dumps({"dependencies": {"x": "^1"}}),
    )
    assert m.scan(tmp_path) == [
        ("npm", "/core/python/poly"),
        ("uv", "/core/python/poly"),
    ]


def test_scan_prunes_vendored_and_build_directories(tmp_path):
    """Without pruning, a committed .venv or node_modules would contribute
    hundreds of bogus directories."""
    _touch(tmp_path / "core/python/alpha/uv.lock")
    _touch(tmp_path / "core/python/alpha/.venv/uv.lock")
    _touch(
        tmp_path / "core/python/alpha/node_modules/pkg/package.json",
        json.dumps({"dependencies": {"y": "^1"}}),
    )
    assert m.scan(tmp_path) == [("uv", "/core/python/alpha")]


def test_scan_ignores_retired_and_unknown_roots(tmp_path):
    """Retired roots (frozen_paths in .github/policy.yml) are closed to new
    work and are not dependency-managed."""
    _touch(tmp_path / "python/agents/legacy/uv.lock")
    _touch(tmp_path / "tools/uv.lock")
    _touch(tmp_path / "uv.lock")
    assert m.scan(tmp_path) == []


def test_scan_tolerates_missing_roots(tmp_path):
    """A checkout need not contain every root — plugins/ did not exist at all
    until the first vertical plugin landed."""
    _touch(tmp_path / "core/python/alpha/uv.lock")
    assert m.scan(tmp_path) == [("uv", "/core/python/alpha")]


def test_scan_is_sorted_and_deduplicated(tmp_path):
    for name in ("zeta", "alpha", "mu"):
        _touch(tmp_path / f"core/python/{name}/uv.lock")
    result = m.scan(tmp_path)
    assert result == sorted(result)
    assert len(result) == len(set(result))


# ---------------------------------------------------------------------------
# Against the real repository
# ---------------------------------------------------------------------------


def test_scan_of_real_repo_finds_recipes():
    """Guards the fail-closed path in close_orphan_dependabot_prs.py: if this
    ever returns nothing, that script refuses to run rather than mass-closing
    PRs, and this test explains why it started refusing."""
    pairs = m.scan()
    assert pairs, "scan found no manifests in the real repo"
    assert any(eco == "uv" for eco, _ in pairs)
    assert all(d.startswith("/") for _, d in pairs)
    assert all(d.split("/")[1] in m.SCAN_ROOTS for _, d in pairs), (
        "every result must live under a declared scan root"
    )
