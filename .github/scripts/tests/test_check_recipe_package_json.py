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
"""Unit tests for check_recipe_package_json.py.

These tests pin the metadata rules for TypeScript recipes:
  - package.json "name" must match folder basename (or <vertical>-<solution> for plugins)
  - package.json "description" must match manifest.description if present
  - "engines.node" must accept the Node 22 CI pin (>=22, ^22, etc.)
  - .npmrc registry= directives must point to public npm registry
  - tsconfig.json is optional; if present, must be valid JSON/JSONC
  - Exit code contract (0 = pass, 1 = violations, 2 = CI fault)
  - Missing .env.example is NOT reported by this checker
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import check_recipe_package_json as m
import pytest

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

MOCK_CHECKOUT = "/mock/repo"


def _write_file(target: Path, content: str | bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _recipe(
    tmp_path: Path,
    package_json: str | bytes | dict | None = None,
    manifest_yaml: str | bytes | None = None,
    npmrc: str | bytes | None = None,
    tsconfig_json: str | bytes | None = None,
) -> Path:
    if package_json is not None:
        if isinstance(package_json, dict):
            _write_file(tmp_path / "package.json", json.dumps(package_json))
        else:
            _write_file(tmp_path / "package.json", package_json)
    if manifest_yaml is not None:
        _write_file(tmp_path / "manifest.yaml", manifest_yaml)
    if npmrc is not None:
        _write_file(tmp_path / ".npmrc", npmrc)
    if tsconfig_json is not None:
        _write_file(tmp_path / "tsconfig.json", tsconfig_json)
    return tmp_path


def _run(tmp_path: Path, monkeypatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_package_json.py", str(tmp_path)]
    )
    return m.main()


# ---------------------------------------------------------------------------
# expected_project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("core/typescript/financial-advisor", "financial-advisor"),
        ("./core/typescript/financial-advisor", "financial-advisor"),
        ("contrib/typescript/financial-advisor", "financial-advisor"),
        ("./plugins/retail/store-ops", "retail-store-ops"),
        ("plugins/retail/store-ops", "retail-store-ops"),
        ("core/../plugins/retail/store-ops", "retail-store-ops"),
        ("financial-advisor", "financial-advisor"),
    ],
)
def test_expected_project_name_for_repo_relative_paths(path, expected):
    assert m.expected_project_name(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{MOCK_CHECKOUT}/core/typescript/financial-advisor",
            "financial-advisor",
        ),
        (
            f"{MOCK_CHECKOUT}/contrib/typescript/financial-advisor",
            "financial-advisor",
        ),
        (
            f"{MOCK_CHECKOUT}/plugins/retail/store-ops",
            "retail-store-ops",
        ),
    ],
)
def test_expected_project_name_for_absolute_paths_inside_repo(path, expected):
    assert (
        m.expected_project_name(Path(path), repo_root=Path(MOCK_CHECKOUT))
        == expected
    )


# ---------------------------------------------------------------------------
# Valid conforming package.json
# ---------------------------------------------------------------------------


def test_valid_package_json_passes(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "typescript" / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "description": "Financial advisor agent",
        "engines": {"node": ">=22"},
    }
    manifest = "description: 'Financial advisor agent'\nlanguage: typescript\n"
    _recipe(recipe_dir, package_json=pkg, manifest_yaml=manifest)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_valid_package_json_without_description_passes(
    tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "contrib" / "typescript" / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_valid_plugin_package_json_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    recipe_dir = tmp_path / "plugins" / "retail" / "store-ops"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "retail-store-ops",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


# ---------------------------------------------------------------------------
# Missing or malformed package.json
# ---------------------------------------------------------------------------


def test_missing_package_json_skips_cleanly(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "typescript" / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    assert _run(recipe_dir, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[SKIP]" in out


def test_non_utf8_package_json_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    _recipe(recipe_dir, package_json=b"\x80\x81\x82 invalid utf-8")

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid UTF-8" in out
    assert "::error" in out


def test_invalid_json_package_json_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    _recipe(recipe_dir, package_json="{ invalid json: }")

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid JSON" in out
    assert "::error" in out


def test_non_dict_json_package_json_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    _recipe(recipe_dir, package_json='["a", "b", "c"]')

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not a JSON object" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# Name checks
# ---------------------------------------------------------------------------


def test_missing_name_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {"engines": {"node": ">=22"}}
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`name` is missing" in out
    assert "::error" in out


def test_mismatched_name_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor-ts",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`name` = 'financial-advisor-ts'" in out
    assert "must declare 'financial-advisor'" in out
    assert "::error" in out


def test_legacy_specimen_shape_mismatched_name_fails(
    tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "customer_service"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "customer-service-ts",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`name` = 'customer-service-ts'" in out
    assert "must declare 'customer_service'" in out


# ---------------------------------------------------------------------------
# Description checks
# ---------------------------------------------------------------------------


def test_description_matches_manifest_passes(tmp_path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "description": "  Multi-agent financial advisor.  ",
        "engines": {"node": ">=22"},
    }
    manifest = "description: 'Multi-agent financial advisor.'\n"
    _recipe(recipe_dir, package_json=pkg, manifest_yaml=manifest)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_description_missing_manifest_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "description": "Some description",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "there is no" in out
    assert "manifest.yaml to check it against" in out


def test_description_invalid_manifest_yaml_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "description": "Some description",
        "engines": {"node": ">=22"},
    }
    manifest = "description: [unclosed list"
    _recipe(recipe_dir, package_json=pkg, manifest_yaml=manifest)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be read as YAML" in out


def test_description_list_manifest_yaml_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "description": "Some description",
        "engines": {"node": ">=22"},
    }
    manifest = "- item1\n- item2\n"
    _recipe(recipe_dir, package_json=pkg, manifest_yaml=manifest)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not a mapping of fields" in out


def test_description_mismatch_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "description": "Description A",
        "engines": {"node": ">=22"},
    }
    manifest = "description: 'Description B'\n"
    _recipe(recipe_dir, package_json=pkg, manifest_yaml=manifest)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "does not match manifest.description" in out
    assert "Description A" in out
    assert "Description B" in out


# ---------------------------------------------------------------------------
# engines.node checks
# ---------------------------------------------------------------------------


def test_missing_engines_block_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {"name": "financial-advisor"}
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`engines.node` is missing" in out


def test_non_dict_engines_block_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {"name": "financial-advisor", "engines": "node >= 22"}
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`engines` in" in out
    assert "requires an object" in out


def test_missing_node_in_engines_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {"name": "financial-advisor", "engines": {"npm": ">=10"}}
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`engines.node` is missing" in out


@pytest.mark.parametrize(
    "node_range",
    [
        ">=22",
        ">=22.0.0",
        ">22.0.0",
        ">21",
        ">=20",
        ">=18.0.0",
        "^22.0.0",
        "~22.0.0",
        "22.x",
        "22.*",
        "22",
        "18.x || 20.x || 22.x",
        "20.x || 22.x",
        ">=20 <24",
        ">=20 <=22",
        "20.0.0 - 22.0.0",
        "18 - 22",
        "*",
    ],
)
def test_valid_engines_node_ranges_pass(node_range, tmp_path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": node_range},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK


@pytest.mark.parametrize(
    "node_range",
    [
        "20.x",
        ">22.x",
        ">22.*",
        ">22",
        "<22",
        "<=21",
        ">=20 <22",
        "^20.0.0",
        "~20.0.0",
        ">=24",
        "<=20",
        "18.x || 20.x",
        "invalid",
    ],
)
def test_invalid_engines_node_ranges_fail(
    node_range, tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": node_range},
    }
    _recipe(recipe_dir, package_json=pkg)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "does not accept Node 22" in out


# ---------------------------------------------------------------------------
# .npmrc registry checks
# ---------------------------------------------------------------------------


def test_valid_npmrc_registry_passes(tmp_path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    npmrc_content = (
        "# Public npm registry\n"
        "registry=https://registry.npmjs.org/\n"
        "@google:registry=https://registry.npmjs.org\n"
        "@scoped:registry=//registry.npmjs.org/\n"
        "@other:registry=//registry.npmjs.org\n"
        "save-exact=true\n"
    )
    _recipe(recipe_dir, package_json=pkg, npmrc=npmrc_content)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_custom_npmrc_registry_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    npmrc_content = "registry=https://npm.pkg.github.com/\n"
    _recipe(recipe_dir, package_json=pkg, npmrc=npmrc_content)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "configures a non-public npm registry" in out
    assert "https://npm.pkg.github.com/" in out


def test_protocol_relative_custom_npmrc_registry_fails(
    tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    npmrc_content = "registry=//npm.pkg.github.com/\n"
    _recipe(recipe_dir, package_json=pkg, npmrc=npmrc_content)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "configures a non-public npm registry" in out
    assert "//npm.pkg.github.com/" in out


def test_scoped_custom_npmrc_registry_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    npmrc_content = "@custom:registry=https://internal.registry.corp/\n"
    _recipe(recipe_dir, package_json=pkg, npmrc=npmrc_content)

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "configures a non-public npm registry" in out
    assert "https://internal.registry.corp/" in out


def test_non_utf8_npmrc_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    _recipe(
        recipe_dir,
        package_json=pkg,
        npmrc=b"\x80\x81 registry=https://registry.npmjs.org/",
    )

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid UTF-8" in out


# ---------------------------------------------------------------------------
# tsconfig.json checks
# ---------------------------------------------------------------------------


def test_valid_tsconfig_passes(tmp_path, monkeypatch):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    tsconfig = """{
      // single line comment
      "compilerOptions": {
        /* multi-line comment */
        "target": "ES2022",
        "module": "ESNext",
        "pattern": "some, } and [foo, ] inside string",
        "types": ["node",],
      },
      "include": ["app", "tests"],
    }"""
    _recipe(recipe_dir, package_json=pkg, tsconfig_json=tsconfig)

    assert _run(recipe_dir, monkeypatch) == EXIT_OK


def test_invalid_json_tsconfig_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg, tsconfig_json="{ compilerOptions: ")

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be parsed as JSON" in out


def test_non_dict_tsconfig_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg, tsconfig_json='["not", "a", "dict"]')

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not a JSON object" in out


def test_non_utf8_tsconfig_fails(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg, tsconfig_json=b'\x80\x81 { "a": 1 }')

    assert _run(recipe_dir, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid UTF-8" in out


# ---------------------------------------------------------------------------
# Missing .env.example is not reported
# ---------------------------------------------------------------------------


def test_missing_env_example_is_not_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    pkg = {
        "name": "financial-advisor",
        "engines": {"node": ">=22"},
    }
    _recipe(recipe_dir, package_json=pkg)
    # Ensure no .env.example
    env_example = recipe_dir / ".env.example"
    assert not env_example.exists()

    assert _run(recipe_dir, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert ".env.example" not in out
    assert "[PASS]" in out


# ---------------------------------------------------------------------------
# CLI arguments & InfraFault
# ---------------------------------------------------------------------------


def test_main_with_no_arguments_returns_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_recipe_package_json.py"])
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "expected exactly one recipe directory" in out


def test_main_with_too_many_arguments_returns_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_package_json.py", "dir1", "dir2"]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "expected exactly one recipe directory" in out


def test_main_with_nonexistent_directory_returns_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_package_json.py", "/nonexistent/path/xyz"]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "is not a directory" in out
