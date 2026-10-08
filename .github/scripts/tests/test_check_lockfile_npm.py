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
"""Unit tests for check_lockfile_npm.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import check_lockfile_npm as m

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2


def _write_lockfile(
    recipe_dir: Path,
    package_lock: dict | str | None,
    package_json: dict | None = None,
    alt_lockfile: str | None = None,
) -> None:
    recipe_dir.mkdir(parents=True, exist_ok=True)
    if package_lock is not None:
        target = recipe_dir / "package-lock.json"
        if isinstance(package_lock, dict):
            target.write_text(
                json.dumps(package_lock, indent=2), encoding="utf-8"
            )
        else:
            target.write_text(package_lock, encoding="utf-8")

    if package_json is not None:
        (recipe_dir / "package.json").write_text(
            json.dumps(package_json, indent=2), encoding="utf-8"
        )

    if alt_lockfile is not None:
        (recipe_dir / alt_lockfile).write_text(
            "lockfile data", encoding="utf-8"
        )


def _run(recipe_dir: Path, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_lockfile_npm.py", str(recipe_dir)])
    return m.main()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_valid_lockfile_v3_passes(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "financial-advisor",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "financial-advisor",
                    "version": "1.0.0",
                    "dependencies": {"@google/adk": "2.2.0"},
                },
                "node_modules/@google/adk": {
                    "version": "2.2.0",
                    "resolved": "https://registry.npmjs.org/@google/adk/-/adk-2.2.0.tgz",
                    "integrity": "sha512-abcdef1234567890==",
                },
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_root_and_workspace_entries_without_integrity_pass(
    tmp_path: Path, monkeypatch
):
    recipe_dir = tmp_path / "workspace-recipe"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "workspace-recipe",
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "workspace-recipe",
                    "workspaces": ["packages/*"],
                },
                "packages/shared-lib": {
                    "version": "1.0.0",
                },
                "node_modules/express": {
                    "version": "4.18.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                    "integrity": "sha512-validhash==",
                },
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_private_registry_is_reported(tmp_path: Path, monkeypatch, capsys):
    recipe_dir = tmp_path / "private-pkg"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "private-pkg",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/internal-helper": {
                    "version": "1.0.0",
                    "resolved": "https://npm.pkg.github.com/internal-helper/-/internal-helper-1.0.0.tgz",
                    "integrity": "sha512-validhash==",
                }
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "non-public npm registry" in out
    assert "https://npm.pkg.github.com" in out


def test_attacker_subdomain_is_reported(tmp_path: Path, monkeypatch, capsys):
    recipe_dir = tmp_path / "attacker-subdomain"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "attacker-subdomain",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/fake-pkg": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org.attacker.example/fake-pkg/-/fake-pkg-1.0.0.tgz",
                    "integrity": "sha512-validhash==",
                }
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "non-public npm registry" in out
    assert "registry.npmjs.org.attacker.example" in out


def test_git_vcs_dependency_is_reported(tmp_path: Path, monkeypatch, capsys):
    recipe_dir = tmp_path / "git-dep"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "git-dep",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/adk-git": {
                    "version": "1.0.0",
                    "resolved": "git+https://github.com/google/adk-js.git#abc123",
                    "integrity": "sha512-validhash==",
                }
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Git/VCS dependency" in out


def test_local_path_and_link_dependency_is_reported(
    tmp_path: Path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "local-dep"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "local-dep",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/local-pkg": {
                    "version": "1.0.0",
                    "resolved": "file:../local-pkg",
                },
                "node_modules/linked-pkg": {
                    "link": True,
                },
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "local path dependency" in out or "local link dependency" in out


def test_missing_integrity_is_reported(tmp_path: Path, monkeypatch, capsys):
    recipe_dir = tmp_path / "no-hash"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "no-hash",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/express": {
                    "version": "4.18.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                }
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "missing an `integrity` hash" in out


def test_malformed_integrity_is_reported(tmp_path: Path, monkeypatch, capsys):
    recipe_dir = tmp_path / "bad-hash"
    _write_lockfile(
        recipe_dir,
        package_lock={
            "name": "bad-hash",
            "lockfileVersion": 3,
            "packages": {
                "node_modules/express": {
                    "version": "4.18.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                    "integrity": "md5-not-allowed",
                }
            },
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "malformed integrity hash" in out


def test_truncated_lockfile_produces_one_shape_error(
    tmp_path: Path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "truncated"
    _write_lockfile(
        recipe_dir,
        package_lock='{ "name": "truncated", "lockfileVersion": 3, "packages": { "node_modules/a": {',
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be parsed as JSON" in out
    assert out.count("::error") == 1


def test_unsupported_lockfile_format_is_reported(
    tmp_path: Path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "pnpm-recipe"
    _write_lockfile(
        recipe_dir,
        package_lock=None,
        alt_lockfile="pnpm-lock.yaml",
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "unsupported lockfile format" in out
    assert "pnpm-lock.yaml" in out


def test_missing_lockfile_with_package_dependencies_fails(
    tmp_path: Path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "no-lockfile"
    _write_lockfile(
        recipe_dir,
        package_lock=None,
        package_json={
            "name": "no-lockfile",
            "dependencies": {"express": "^4.18.2"},
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "has no package-lock.json" in out


def test_no_dependencies_and_no_lockfile_passes(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "clean-recipe"
    _write_lockfile(
        recipe_dir,
        package_lock=None,
        package_json={"name": "clean-recipe"},
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_real_specimen_passes(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib" / "typescript" / "financial-advisor"

    assert specimen.exists(), f"Specimen not found at {specimen}"
    assert _run(specimen, monkeypatch) == EXIT_OK


def test_ci_faults(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_lockfile_npm.py"])
    assert m.main() == EXIT_CI_FAULT

    monkeypatch.setattr(
        sys, "argv", ["check_lockfile_npm.py", str(tmp_path / "nonexistent")]
    )
    assert m.main() == EXIT_CI_FAULT
