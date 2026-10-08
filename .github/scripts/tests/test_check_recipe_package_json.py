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
"""Unit tests for check_recipe_package_json.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_recipe_package_json as m

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2


def _write_files(
    recipe_dir: Path,
    package_json: dict | str | None,
    manifest_yaml: str | None = None,
    npmrc: str | None = None,
    tsconfig: dict | str | None = None,
) -> None:
    recipe_dir.mkdir(parents=True, exist_ok=True)
    if package_json is not None:
        if isinstance(package_json, dict):
            (recipe_dir / "package.json").write_text(
                json.dumps(package_json, indent=2), encoding="utf-8"
            )
        else:
            (recipe_dir / "package.json").write_text(
                package_json, encoding="utf-8"
            )

    if manifest_yaml is not None:
        (recipe_dir / "manifest.yaml").write_text(
            manifest_yaml, encoding="utf-8"
        )

    if npmrc is not None:
        (recipe_dir / ".npmrc").write_text(npmrc, encoding="utf-8")

    if tsconfig is not None:
        if isinstance(tsconfig, dict):
            (recipe_dir / "tsconfig.json").write_text(
                json.dumps(tsconfig, indent=2), encoding="utf-8"
            )
        else:
            (recipe_dir / "tsconfig.json").write_text(
                tsconfig, encoding="utf-8"
            )


def _run(recipe_dir: Path, monkeypatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_package_json.py", str(recipe_dir)]
    )
    return m.main()


# ---------------------------------------------------------------------------
# node_engine_accepts_node_22
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine_range",
    [
        ">=22",
        ">=22.0.0",
        ">=20",
        ">=20.0.0",
        ">=18",
        ">=18.0.0",
        "^22",
        "^22.0.0",
        "~22",
        "~22.0.0",
        "22.x",
        "22.*",
        "22",
        "*",
        "x",
        "latest",
        "20.x || 22.x",
        ">=20 || >=22",
        ">=18 <=22",
        ">=20.0.0 <23.0.0",
    ],
)
def test_node_engine_accepts_valid_node_22_ranges(engine_range: str):
    assert m.node_engine_accepts_node_22(engine_range) is True


@pytest.mark.parametrize(
    "engine_range",
    [
        "<20",
        "<22",
        "<=21",
        "^22.1.0",
        "20.x",
        "20.*",
        "^20",
        "^20.0.0",
        "~20.0.0",
        ">=23",
        ">=24",
        "23.x",
        "18.x || 20.x",
    ],
)
def test_node_engine_rejects_invalid_node_22_ranges(engine_range: str):
    assert m.node_engine_accepts_node_22(engine_range) is False


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


def test_conforming_recipe_passes(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "customer-service"
    _write_files(
        recipe_dir,
        package_json={
            "name": "customer-service",
            "version": "1.0.0",
            "description": "Customer service agent recipe.",
            "engines": {"node": ">=22"},
        },
        manifest_yaml=(
            "name: customer-service\n"
            "description: Customer service agent recipe.\n"
            "language: typescript\n"
        ),
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_missing_package_json_exits_0(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "my-recipe"
    recipe_dir.mkdir(parents=True)
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_no_env_example_is_not_reported(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "sample-agent"
    _write_files(
        recipe_dir,
        package_json={
            "name": "sample-agent",
            "version": "1.0.0",
            "engines": {"node": ">=22"},
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_name_mismatch_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "customer_service"
    _write_files(
        recipe_dir,
        package_json={
            "name": "customer-service-ts",
            "engines": {"node": ">=22"},
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_missing_name_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "customer-service"
    _write_files(
        recipe_dir,
        package_json={
            "version": "1.0.0",
            "engines": {"node": ">=22"},
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_description_mismatch_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    _write_files(
        recipe_dir,
        package_json={
            "name": "financial-advisor",
            "description": "Short description.",
            "engines": {"node": ">=22"},
        },
        manifest_yaml=(
            "name: financial-advisor\n"
            "description: Completely different manifest description.\n"
            "language: typescript\n"
        ),
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_description_present_but_manifest_missing_fails(
    tmp_path: Path, monkeypatch
):
    recipe_dir = tmp_path / "financial-advisor"
    _write_files(
        recipe_dir,
        package_json={
            "name": "financial-advisor",
            "description": "Some description.",
            "engines": {"node": ">=22"},
        },
        manifest_yaml=None,
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_description_absent_in_package_json_passes(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    _write_files(
        recipe_dir,
        package_json={
            "name": "financial-advisor",
            "engines": {"node": ">=22"},
        },
        manifest_yaml=(
            "name: financial-advisor\n"
            "description: Manifest description.\n"
            "language: typescript\n"
        ),
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_invalid_manifest_yaml_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    _write_files(
        recipe_dir,
        package_json={
            "name": "financial-advisor",
            "description": "Some description.",
            "engines": {"node": ">=22"},
        },
        manifest_yaml="name: [invalid yaml\n",
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_missing_engines_node_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json={
            "name": "loan-agent",
            "version": "1.0.0",
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_incompatible_engines_node_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json={
            "name": "loan-agent",
            "engines": {"node": "20.x"},
        },
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_public_npmrc_passes(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json={
            "name": "loan-agent",
            "engines": {"node": ">=22"},
        },
        npmrc="registry=https://registry.npmjs.org/\n",
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_private_npmrc_registry_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json={
            "name": "loan-agent",
            "engines": {"node": ">=22"},
        },
        npmrc="registry=https://npm.corp.internal.example.com/\n",
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_malformed_json_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json="{ invalid json",
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_non_object_json_fails(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json='["not", "an", "object"]',
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS


def test_optional_tsconfig_json_tolerated(tmp_path: Path, monkeypatch):
    recipe_dir = tmp_path / "loan-agent"
    _write_files(
        recipe_dir,
        package_json={
            "name": "loan-agent",
            "engines": {"node": ">=22"},
        },
        tsconfig={"compilerOptions": {"target": "ES2022"}},
    )
    assert _run(recipe_dir, monkeypatch) == EXIT_OK
