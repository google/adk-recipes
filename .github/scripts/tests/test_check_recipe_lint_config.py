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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_recipe_lint_config as m

EXIT_OK = 0
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
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[NOTICE]" in out
    assert "Biome configuration" in out
    assert "biome.json" in out
    assert f"::warning file={tmp_path / 'biome.json'}::" in out


def test_recipe_with_nested_biome_json_fails(tmp_path, monkeypatch, capsys):
    sub = tmp_path / "frontend" / "nested"
    sub.mkdir(parents=True)
    (sub / "biome.json").write_text("{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "biome.json" in out
    assert f"::warning file={sub / 'biome.json'}::" in out


def test_recipe_with_biome_jsonc_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / "biome.jsonc").write_text("// config\n{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "biome.jsonc" in out
    assert f"::warning file={tmp_path / 'biome.jsonc'}::" in out


def test_recipe_with_biomerc_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".biomerc").write_text("{}\n")
    (tmp_path / ".biomerc.json").write_text("{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert ".biomerc" in out
    assert ".biomerc.json" in out
    assert out.count("::warning file=") == 2


def test_recipe_with_golangci_yml_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.yml").write_text(
        "linters:\n  enable:\n    - errcheck\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[NOTICE]" in out
    assert "golangci-lint configuration" in out
    assert ".golangci.yml" in out
    assert f"::warning file={tmp_path / '.golangci.yml'}::" in out


def test_recipe_with_golangci_yaml_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.yaml").write_text(
        "linters:\n  enable:\n    - gofmt\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert ".golangci.yaml" in out
    assert f"::warning file={tmp_path / '.golangci.yaml'}::" in out


def test_recipe_with_golangci_toml_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.toml").write_text("[linters]\nenable = ['govet']\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert ".golangci.toml" in out
    assert f"::warning file={tmp_path / '.golangci.toml'}::" in out


def test_recipe_with_nested_golangci_fails(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "pkg" / "util"
    pkg.mkdir(parents=True)
    (pkg / ".golangci.yml").write_text("linters:\n  enable:\n    - govet\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert ".golangci.yml" in out
    assert f"::warning file={pkg / '.golangci.yml'}::" in out


def test_recipe_with_editorconfig_kotlin_section_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.{kt,kts}]\nindent_size = 4\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[NOTICE]" in out
    assert ".editorconfig" in out
    assert "[*.{kt,kts}]" in out
    assert f"::warning file={tmp_path / '.editorconfig'}::" in out


def test_recipe_with_editorconfig_kt_single_extension_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.kt]\nindent_size = 4\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[*.kt]" in out


def test_recipe_with_editorconfig_kts_single_extension_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*.kts]\nindent_size = 4\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
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


def test_recipe_with_editorconfig_ktlint_property_in_generic_section_fails(
    tmp_path, monkeypatch, capsys
):
    # ktlint applies [*] to Kotlin files, so a ktlint_* property there
    # overrides the repo style even with no Kotlin-specific section.
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*]\nktlint_standard_no-wildcard-imports = disabled\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[NOTICE]" in out
    assert "Kotlin property ktlint_standard_no-wildcard-imports" in out
    assert "section [*]" in out
    assert f"::warning file={tmp_path / '.editorconfig'}::" in out


def test_recipe_with_editorconfig_ktlint_property_in_preamble_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "ktlint_code_style = ktlint_official\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "Kotlin property ktlint_code_style in the preamble" in out


def test_recipe_with_editorconfig_generic_section_without_ktlint_passes(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n\n[*]\nindent_style = space\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::warning" not in out


def test_recipe_with_golangci_json_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".golangci.json").write_text('{"linters": {}}\n')
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert f"::warning file={tmp_path / '.golangci.json'}::" in out


def test_recipe_with_editorconfig_ij_kotlin_property_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text(
        "[*]\nij_kotlin_allow_trailing_comma = false\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert (
        "Kotlin property ij_kotlin_allow_trailing_comma in section [*]" in out
    )


@pytest.mark.parametrize("section", ["*", "**", "src/**"])
def test_kotlin_recipe_with_generic_max_line_length_fails(
    tmp_path, monkeypatch, capsys, section
):
    # ktlint honours max_line_length from any section matching a .kt file.
    (tmp_path / "Main.kt").write_text("fun main() {}\n")
    (tmp_path / ".editorconfig").write_text(
        f"root = true\n\n[{section}]\nmax_line_length = 140\n"
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert f"property max_line_length in section [{section}]" in out
    assert f"::warning file={tmp_path / '.editorconfig'}::" in out


def test_non_kotlin_recipe_with_generic_max_line_length_passes(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / ".editorconfig").write_text("[*]\nmax_line_length = 140\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out


def test_kotlin_recipe_with_non_kotlin_section_property_passes(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "Main.kt").write_text("fun main() {}\n")
    (tmp_path / ".editorconfig").write_text("[*.md]\nmax_line_length = 140\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out


def test_editorconfig_mixed_brace_list_with_kt_fails(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".editorconfig").write_text("[*.{java,kt}]\nindent_size = 4\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "Kotlin style section [*.{java,kt}]" in out


@pytest.mark.parametrize("section", ["docs/kt-notes.md", "*.ktx", "kt/*.py"])
def test_editorconfig_section_merely_mentioning_kt_passes(
    tmp_path, monkeypatch, capsys, section
):
    (tmp_path / ".editorconfig").write_text(f"[{section}]\nindent_size = 4\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out


def test_findings_are_advisory_not_errors(tmp_path, monkeypatch, capsys):
    # During the advisory rollout a finding must not render as a red
    # annotation on a green run.
    (tmp_path / "biome.json").write_text("{}\n")
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "::warning file=" in out
    assert "::error" not in out
    assert "[FAIL]" not in out


def test_root_level_configs_not_reported():
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

    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert out.count("::warning file=") == 3


def test_clean_kotlin_recipe_passes(tmp_path, monkeypatch, capsys):
    (tmp_path / "Main.kt").write_text('fun main() { println("hello") }\n')
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_real_kotlin_recipe_passes_clean(monkeypatch, capsys):
    kotlin_recipe = REPO_ROOT / "core" / "kotlin" / "llm-auditor"
    if not kotlin_recipe.is_dir():
        pytest.skip("core/kotlin/llm-auditor not present in this workspace")
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
