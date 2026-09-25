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
"""Unit tests for check_recipe_lint_config.py."""

from __future__ import annotations

import sys
from pathlib import Path

import check_recipe_lint_config as m

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(recipe_dir: Path, monkeypatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_lint_config.py", str(recipe_dir)]
    )
    return m.main()


def test_clean_recipe_passes(tmp_path, monkeypatch, capsys):
    (tmp_path / "main.ts").write_text("console.log('hello');\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_recipe_with_biome_json_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / "biome.json").write_text("{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "Biome configuration" in out
    assert "biome.json" in out
    assert f"::error file={tmp_path / 'biome.json'}::" in out


def test_recipe_with_nested_biome_json_fails(tmp_path, monkeypatch, capsys):
    sub = tmp_path / "frontend" / "nested"
    sub.mkdir(parents=True)
    (sub / "biome.json").write_text("{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "biome.json" in out
    assert f"::error file={sub / 'biome.json'}::" in out


def test_recipe_with_biome_jsonc_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / "biome.jsonc").write_text("// config\n{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "biome.jsonc" in out
    assert f"::error file={tmp_path / 'biome.jsonc'}::" in out


def test_recipe_with_biomerc_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".biomerc").write_text("{}\n")
    (tmp_path / ".biomerc.json").write_text("{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert ".biomerc" in out
    assert ".biomerc.json" in out
    assert out.count("::error file=") == 2


def test_recipe_with_golangci_yml_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.yml").write_text(
        "linters:\n  enable:\n    - errcheck\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "golangci-lint configuration" in out
    assert ".golangci.yml" in out
    assert f"::error file={tmp_path / '.golangci.yml'}::" in out


def test_recipe_with_golangci_yaml_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.yaml").write_text(
        "linters:\n  enable:\n    - gofmt\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert ".golangci.yaml" in out
    assert f"::error file={tmp_path / '.golangci.yaml'}::" in out


def test_recipe_with_golangci_toml_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.toml").write_text("[linters]\nenable = ['govet']\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert ".golangci.toml" in out
    assert f"::error file={tmp_path / '.golangci.toml'}::" in out


def test_recipe_with_nested_golangci_fails(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "pkg" / "util"
    pkg.mkdir(parents=True)
    (pkg / ".golangci.yml").write_text("linters:\n  enable:\n    - govet\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert ".golangci.yml" in out
    assert f"::error file={pkg / '.golangci.yml'}::" in out


def test_recipe_with_editorconfig_kotlin_section_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.{kt,kts}]\nindent_size = 4\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert ".editorconfig" in out
    assert "[*.{kt,kts}]" in out
    assert f"::error file={tmp_path / '.editorconfig'}::" in out


def test_recipe_with_editorconfig_kt_single_extension_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.kt]\nindent_size = 4\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[*.kt]" in out


def test_recipe_with_editorconfig_kts_single_extension_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.kts]\nindent_size = 4\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[*.kts]" in out


def test_recipe_with_editorconfig_non_kotlin_section_passes(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.py]\nindent_size = 4\n\n[*.md]\nindent_size = 2\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_root_level_configs_not_reported(monkeypatch, capsys):
    # When check_recipe_lint_config runs with REPO_ROOT as the root,
    # root-level configs (biome.json, .golangci.yml) must not be reported.
    violations = m._collect_violations(REPO_ROOT, repo_root=REPO_ROOT)
    # Filter out anything not directly in REPO_ROOT
    root_violations = [
        v
        for v in violations
        if v.file in ("biome.json", "biome.jsonc", ".golangci.yml")
    ]
    assert not root_violations


def test_multiple_violations_each_get_annotation(tmp_path, monkeypatch, capsys):
    (tmp_path / "biome.json").write_text("{}\n")
    (tmp_path / ".golangci.yml").write_text("linters:\n  enable: [govet]\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".editorconfig").write_text("[*.{kt,kts}]\nindent_size = 4\n")

    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert out.count("::error file=") == 3


def test_real_kotlin_recipe_passes_clean(monkeypatch, capsys):
    kotlin_recipe = REPO_ROOT / "core" / "kotlin" / "llm-auditor"
    assert kotlin_recipe.is_dir()
    assert _run(kotlin_recipe, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_path_not_a_directory_is_ci_fault(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path / "nonexistent", monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "is not a directory" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_recipe_lint_config.py"])
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "expected exactly one recipe directory" in out


def test_unexpected_crash_is_ci_fault(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated checker failure")

    monkeypatch.setattr(m, "_collect_violations", boom)
    assert _run(tmp_path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "RuntimeError: simulated checker failure" in out
    assert "::error file=" not in out
