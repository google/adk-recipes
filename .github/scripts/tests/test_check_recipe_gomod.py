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
"""Unit tests for check_recipe_gomod.py.

These tests pin the metadata rules for Go recipes:
  - Canonical module path matching repo location
  - Go version directive <= CI pinned version (1.26)
  - No local filesystem replace directives
  - Exit code contract (0 = pass, 1 = violations, 2 = CI fault)
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_recipe_gomod as m
import pytest

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

CI_CHECKOUT = "/home/runner/work/adk-recipes/adk-recipes"


def _run(tmp_path: Path, monkeypatch, gomod_content: str | bytes | None) -> int:
    """Invoke the checker against tmp_path."""
    if gomod_content is not None:
        target = tmp_path / "go.mod"
        if isinstance(gomod_content, bytes):
            target.write_bytes(gomod_content)
        else:
            target.write_text(gomod_content, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_recipe_gomod.py", str(tmp_path)])
    return m.main()


# ---------------------------------------------------------------------------
# expected_module_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "core/go/financial-advisor",
            "github.com/google/adk-recipes/core/go/financial-advisor",
        ),
        (
            "contrib/go/financial-advisor",
            "github.com/google/adk-recipes/contrib/go/financial-advisor",
        ),
        (
            "contrib/go/deep-search",
            "github.com/google/adk-recipes/contrib/go/deep-search",
        ),
        (
            "financial-advisor",
            "github.com/google/adk-recipes/contrib/go/financial-advisor",
        ),
    ],
)
def test_expected_module_path_for_repo_relative_paths(path, expected):
    assert m.expected_module_path(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{CI_CHECKOUT}/core/go/sample-app",
            "github.com/google/adk-recipes/core/go/sample-app",
        ),
        (
            f"{CI_CHECKOUT}/contrib/go/financial-advisor",
            "github.com/google/adk-recipes/contrib/go/financial-advisor",
        ),
    ],
)
def test_expected_module_path_for_absolute_paths_inside_repo(path, expected):
    assert (
        m.expected_module_path(Path(path), repo_root=Path(CI_CHECKOUT))
        == expected
    )


# ---------------------------------------------------------------------------
# Valid go.mod
# ---------------------------------------------------------------------------


def test_valid_gomod_passes(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/financial-advisor

go 1.26.0

require (
	github.com/joho/godotenv v1.5.1
	google.golang.org/adk v1.7.0
)
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_valid_gomod_with_lower_go_version_passes(
    tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "core" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/core/go/my-recipe

go 1.25.0

require github.com/joho/godotenv v1.5.1
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_valid_gomod_with_published_module_replace_passes(
    tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe

go 1.26

require example.com/foo v1.0.0

replace example.com/foo v1.0.0 => example.com/forked/foo v1.0.1
replace (
    example.com/bar => example.com/forked/bar v2.0.0
)
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_valid_gomod_with_comments_and_quotes_passes(
    tmp_path, monkeypatch, capsys
):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """// Package description comment
module "github.com/google/adk-recipes/contrib/go/my-recipe" // inline comment

go 1.26 // go directive

require (
    // Dependency
    `github.com/joho/godotenv` v1.5.1
    "example.com/escaped\\\"name" v1.0.0
)
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Module path rule
# ---------------------------------------------------------------------------


def test_missing_module_directive_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """go 1.26.0
require github.com/joho/godotenv v1.5.1
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-module-path]" in out
    assert "`module` directive is missing" in out
    assert f"::error file={recipe_dir / 'go.mod'}::" in out


def test_wrong_module_path_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "financial-advisor"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-samples/go/agents/financial-advisor

go 1.26.0
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-module-path]" in out
    assert "github.com/google/adk-samples/go/agents/financial-advisor" in out
    assert "github.com/google/adk-recipes/contrib/go/financial-advisor" in out
    assert f"::error file={recipe_dir / 'go.mod'}::" in out


def test_module_path_with_block_form(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module (
    github.com/google/adk-recipes/contrib/go/my-recipe
)

go 1.26.0
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Go version rule
# ---------------------------------------------------------------------------


def test_missing_go_directive_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-go-version]" in out
    assert "`go` directive is missing" in out


def test_go_version_higher_than_ci_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe

go 1.27.0
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-go-version]" in out
    assert "exceeds CI's pinned Go version (1.26)" in out


def test_invalid_go_version_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe

go not-a-version
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-go-version]" in out
    assert "not a valid version" in out


# ---------------------------------------------------------------------------
# Replace directives rule
# ---------------------------------------------------------------------------


def test_single_line_local_replace_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe

go 1.26.0

replace example.com/local => ../local/path
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-no-local-replace]" in out
    assert "../local/path" in out
    assert f"::error file={recipe_dir / 'go.mod'}::" in out


def test_block_form_local_replace_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe

go 1.26.0

replace (
    example.com/one => ./relative/one
    example.com/two => /absolute/two
    example.com/published => example.com/forked v1.0.0
)
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-no-local-replace]" in out
    assert "./relative/one" in out
    assert "/absolute/two" in out
    assert "example.com/forked" not in out
    # 2 annotations for the 2 local replaces
    assert out.count("::error file=") == 2


# ---------------------------------------------------------------------------
# Syntax & Parsing
# ---------------------------------------------------------------------------


def test_malformed_replace_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = """module github.com/google/adk-recipes/contrib/go/my-recipe

go 1.26.0

replace example.com/missing-arrow
"""
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-parse]" in out
    assert "Malformed replace directive" in out


def test_non_utf8_gomod_reported(tmp_path, monkeypatch, capsys):
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    content = "module caf\u00e9\n".encode("latin-1")
    assert _run(recipe_dir, monkeypatch, content) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[gomod-parse]" in out
    assert "not valid UTF-8" in out


def test_missing_gomod_skipped(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, None) == EXIT_OK
    out = capsys.readouterr().out
    assert "[SKIP]" in out
    assert "::error" not in out


# ---------------------------------------------------------------------------
# CI Faults
# ---------------------------------------------------------------------------


def test_non_directory_arg_is_ci_fault(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_gomod.py", str(tmp_path / "nonexistent")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_unexpected_crash_is_ci_fault(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(m, "parse_gomod", boom)
    recipe_dir = tmp_path / "contrib" / "go" / "my-recipe"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "go.mod").write_text("module foo\n", encoding="utf-8")

    assert _run(recipe_dir, monkeypatch, None) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "RuntimeError: simulated bug" in out
    assert "::error file=" not in out
