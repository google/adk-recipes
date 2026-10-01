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
"""Unit tests for check_lockfile_gosum.py.

Verifies that Go recipe go.sum checksums are validated against go.mod:
  - Missing go.sum when dependencies are required is reported (exit 1).
  - Missing h1: content hashes for required modules are reported (exit 1).
  - Malformed lines in go.sum are reported (exit 1).
  - Recipes with no requires and no go.sum pass silently (exit 0).
  - Stale go.sum files when no dependencies are required are reported (exit 1).
  - The financial-advisor Go specimen parses clean (exit 0).
  - CI faults (bad CLI arguments, missing paths, crashes) exit 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_lockfile_gosum as m

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

# Valid SHA-256 base64 hashes (43 chars + '=')
_VALID_HASH_1 = "h1:7eLL/+HRGLY0ldzfGMeQkb7vMd0as4CfYvUVzLqw0N0="
_VALID_HASH_2 = "h1:EHSlil6b/KG3OquU+uKDA4cvz/Fyn8Nsch5MysXEehc="


def _write_recipe(
    tmp_path: Path,
    go_mod: str,
    go_sum: str | None = None,
) -> Path:
    (tmp_path / "go.mod").write_text(go_mod, encoding="utf-8")
    if go_sum is not None:
        (tmp_path / "go.sum").write_text(go_sum, encoding="utf-8")
    return tmp_path


def _run(target: Path | str, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_lockfile_gosum.py", str(target)])
    return m.main()


def test_specimen_financial_advisor_parses_clean(monkeypatch, capsys):
    """The specimen Go recipe in the repository must pass all checks."""
    repo_root = Path(__file__).resolve().parents[3]
    # Check contrib/go/financial-advisor or go/agents/financial-advisor
    specimen = repo_root / "contrib" / "go" / "financial-advisor"
    if not specimen.exists():
        specimen = repo_root / "go" / "agents" / "financial-advisor"

    assert specimen.exists(), f"Specimen not found at {specimen}"
    assert _run(specimen, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_recipe_with_no_requires_and_no_gosum_passes_silently(
    tmp_path, monkeypatch, capsys
):
    path = _write_recipe(
        tmp_path,
        "module github.com/example/recipe\n\ngo 1.25.0\n",
    )
    assert _run(path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_recipe_with_valid_requires_and_gosum_passes(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require (\n"
        "    github.com/joho/godotenv v1.5.1\n"
        "    google.golang.org/adk v1.7.0 // indirect\n"
        ")\n"
    )
    go_sum = (
        f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
        f"github.com/joho/godotenv v1.5.1/go.mod {_VALID_HASH_1}\n"
        f"google.golang.org/adk v1.7.0 {_VALID_HASH_2}\n"
        f"google.golang.org/adk v1.7.0/go.mod {_VALID_HASH_2}\n"
    )
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_missing_gosum_when_requires_exist_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    path = _write_recipe(tmp_path, go_mod)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "go.sum is missing" in out
    assert "go mod tidy" in out
    assert f"::error file={tmp_path / 'go.sum'}::" in out


def test_require_entry_with_no_matching_gosum_hash_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require (\n"
        "    github.com/joho/godotenv v1.5.1\n"
        "    google.golang.org/adk v1.7.0\n"
        ")\n"
    )
    # go.sum only has godotenv, missing google.golang.org/adk
    go_sum = (
        f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
        f"github.com/joho/godotenv v1.5.1/go.mod {_VALID_HASH_1}\n"
    )
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "google.golang.org/adk v1.7.0" in out
    assert "no matching h1: content hash" in out
    assert f"::error file={tmp_path / 'go.sum'}::" in out


def test_require_entry_version_mismatch_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    # go.sum has older v1.4.0, not the required v1.5.1
    go_sum = f"github.com/joho/godotenv v1.4.0 {_VALID_HASH_1}\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "github.com/joho/godotenv v1.5.1" in out
    assert "no matching h1: content hash" in out


def test_require_entry_with_only_gomod_hash_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    # Only v1.5.1/go.mod hash, missing module content hash v1.5.1
    go_sum = f"github.com/joho/godotenv v1.5.1/go.mod {_VALID_HASH_1}\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "github.com/joho/godotenv v1.5.1" in out
    assert "no matching h1: content hash" in out


def test_malformed_gosum_hash_is_reported(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    go_sum = "github.com/joho/godotenv v1.5.1 md5:deadbeef\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "malformed entry" in out
    assert "md5:deadbeef" in out


def test_malformed_gosum_token_count_is_reported(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    go_sum = "github.com/joho/godotenv v1.5.1\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "malformed entry" in out


def test_malformed_gosum_version_not_starting_with_v_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    go_sum = f"github.com/joho/godotenv 1.5.1 {_VALID_HASH_1}\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "malformed entry" in out


def test_stale_gosum_with_no_requires_in_gomod_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = "module github.com/example/recipe\n\ngo 1.25.0\n"
    go_sum = f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "no require entries" in out
    assert f"::error file={tmp_path / 'go.sum'}::" in out


def test_stale_empty_gosum_with_no_requires_in_gomod_is_reported(
    tmp_path, monkeypatch, capsys
):
    go_mod = "module github.com/example/recipe\n\ngo 1.25.0\n"
    go_sum = ""
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "no require entries" in out
    assert f"::error file={tmp_path / 'go.sum'}::" in out


def test_gomod_syntax_error_is_reported(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require (\n"
        "    github.com/joho/godotenv\n"  # missing version
        ")\n"
    )
    path = _write_recipe(tmp_path, go_mod)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "invalid require entry" in out


def test_unclosed_require_block_is_reported(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require (\n"
        "    github.com/joho/godotenv v1.5.1\n"
    )
    path = _write_recipe(tmp_path, go_mod)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Unclosed require block" in out


def test_single_line_require_parentheses_block(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require ( github.com/joho/godotenv v1.5.1 )\n"
    )
    go_sum = f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_OK
    assert "[PASS]" in capsys.readouterr().out


def test_malformed_single_line_require_parentheses_block(
    tmp_path, monkeypatch, capsys
):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require ( github.com/joho/godotenv )\n"  # missing version
    )
    path = _write_recipe(tmp_path, go_mod)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    assert "invalid require entry" in capsys.readouterr().out


def test_quoted_and_multiple_require_blocks(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        'require "github.com/joho/godotenv" "v1.5.1"\n\n'
        "require (\n"
        '    "google.golang.org/adk" v1.7.0 // indirect\n'
        ")\n"
    )
    go_sum = (
        f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
        f"google.golang.org/adk v1.7.0 {_VALID_HASH_2}\n"
    )
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_OK
    assert "[PASS]" in capsys.readouterr().out


def test_quotes_with_slashes_in_gomod(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        'require "github.com/joho//godotenv" v1.5.1 // comment with // inside\n'
    )
    go_sum = f"github.com/joho//godotenv v1.5.1 {_VALID_HASH_1}\n"
    path = _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(path, monkeypatch) == EXIT_OK
    assert "[PASS]" in capsys.readouterr().out


def test_target_passed_as_gomod_path(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    go_sum = f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
    _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(tmp_path / "go.mod", monkeypatch) == EXIT_OK
    assert "[PASS]" in capsys.readouterr().out


def test_target_passed_as_gosum_path(tmp_path, monkeypatch, capsys):
    go_mod = (
        "module github.com/example/recipe\n\n"
        "go 1.25.0\n\n"
        "require github.com/joho/godotenv v1.5.1\n"
    )
    go_sum = f"github.com/joho/godotenv v1.5.1 {_VALID_HASH_1}\n"
    _write_recipe(tmp_path, go_mod, go_sum)
    assert _run(tmp_path / "go.sum", monkeypatch) == EXIT_OK
    assert "[PASS]" in capsys.readouterr().out


def test_nonexistent_target_is_ci_fault(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path / "nonexistent", monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_missing_gomod_in_directory_is_ci_fault(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_lockfile_gosum.py"])
    assert m.main() == EXIT_CI_FAULT
    assert "[ci-fault]" in capsys.readouterr().out


def test_unexpected_crash_is_ci_fault(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(m, "parse_go_mod", boom)
    path = _write_recipe(
        tmp_path, "module github.com/example/recipe\n\ngo 1.25.0\n"
    )
    assert _run(path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "RuntimeError: unexpected parser failure" in out
    assert "::error file=" not in out
