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
"""Unit tests for check_env_vars_go.py.

Verifies that Go recipe environment variable reads (os.Getenv and os.LookupEnv)
are validated against .env.example:
  - Declared variables pass (exit 0).
  - Allowlisted variables (HOME, PATH, CI, GITHUB_*, etc.) are not reported (exit 0).
  - Undeclared variables are reported with file, line, and fix (exit 1).
  - Import aliases (import goos "os") and dot imports (import . "os") are supported.
  - Test files (*_test.go) and test directories are ignored.
  - The financial-advisor Go specimen in the repository passes clean (exit 0).
  - Missing .env.example exits 0 (owned by a separate required-files check).
  - Non-UTF8 files and syntax errors are reported (exit 1).
  - CI faults (bad CLI arguments, missing paths, crashes) exit 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_env_vars_go as m

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2


def _recipe(
    tmp_path: Path, env_example: str | bytes | None, **sources: str
) -> Path:
    if env_example is not None:
        target = tmp_path / ".env.example"
        if isinstance(env_example, bytes):
            target.write_bytes(env_example)
        else:
            target.write_text(env_example, encoding="utf-8")
    for name, body in sources.items():
        path = tmp_path / f"{name}.go"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path | str, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_env_vars_go.py", str(tmp_path)])
    return m.main()


def test_declared_variable_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "package main\n\n"
            'import "os"\n\n'
            "func main() {\n"
            '    _ = os.Getenv("GOOGLE_CLOUD_PROJECT")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_lookup_env_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "PORT=8080\nVAR_WITH_COMMENT=val\n",
        main=(
            "package main\n\n"
            'import "os"\n\n'
            "func main() {\n"
            '    val, ok := os.LookupEnv("PORT")\n'
            '    _ = os.Getenv(/* inline comment */ "VAR_WITH_COMMENT")\n'
            "    _ = val\n"
            "    _ = ok\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_allowlisted_variable_is_not_reported(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "package main\n\n"
            'import "os"\n\n'
            "func main() {\n"
            '    _ = os.Getenv("HOME")\n'
            '    _ = os.Getenv("PATH")\n'
            '    _ = os.Getenv("CI")\n'
            '    _ = os.Getenv("GITHUB_TOKEN")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_undeclared_variable_names_the_file_and_line(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "package main\n"
            "\n"
            'import "os"\n'
            "\n"
            "func main() {\n"
            '    _ = os.Getenv("GOOGLE_CLOUD_PROJECT")\n'
            "    _ = os.Getenv(\n"
            '        "MODEL_NAME",\n'
            "    )\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MODEL_NAME" in out
    # The read starts on line 7
    assert f"{tmp_path / 'main.go'}:7" in out
    # A literal line to paste
    assert f"MODEL_NAME={m.PLACEHOLDER}" in out
    assert f"::error file={tmp_path / '.env.example'}::" in out


def test_each_undeclared_variable_gets_its_own_annotation(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            "package main\n\n"
            'import "os"\n\n'
            "func main() {\n"
            '    _ = os.Getenv("ONE")\n'
            '    _, _ = os.LookupEnv("TWO")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    assert capsys.readouterr().out.count("::error file=") == 2


def test_aliased_import_goos_is_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "package main\n\n"
            'import goos "os"\n\n'
            "func main() {\n"
            '    _ = goos.Getenv("GOOGLE_CLOUD_PROJECT")\n'
            '    _ = goos.Getenv("UNDECLARED_SECRET")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_SECRET" in out
    assert f"{tmp_path / 'main.go'}:7" in out
    assert "GOOGLE_CLOUD_PROJECT" not in out


def test_dot_import_is_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package main\n\n"
            'import . "os"\n\n'
            "func main() {\n"
            '    _ = Getenv("DECLARED_VAR")\n'
            '    _, _ = LookupEnv("UNDECLARED_DOT_VAR")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_DOT_VAR" in out
    assert f"{tmp_path / 'main.go'}:7" in out
    assert "DECLARED_VAR" not in out


def test_raw_string_literal_in_import_and_call(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package main\n\n"
            "import `os`\n\n"
            "func main() {\n"
            "    _ = os.Getenv(`DECLARED_VAR`)\n"
            "    _ = os.Getenv(`UNDECLARED_RAW_VAR`)\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_RAW_VAR" in out
    assert "DECLARED_VAR" not in out


def test_test_files_and_directories_are_ignored(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=val\n",
        main=(
            "package main\n\n"
            'import "os"\n\n'
            "func main() {\n"
            '    _ = os.Getenv("DECLARED_VAR")\n'
            "}\n"
        ),
        agent_test=(
            "package main\n\n"
            'import "os"\n\n'
            "func TestSomething() {\n"
            '    _ = os.Getenv("TEST_ONLY_SECRET")\n'
            "}\n"
        ),
        runnability_test=(
            "package main\n\n"
            'import "os"\n\n'
            "func TestRunnability() {\n"
            '    _ = os.Getenv("RUNNABILITY_ONLY_SECRET")\n'
            "}\n"
        ),
    )
    # Also write a file in tests/ subdirectory
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "helper.go").write_text(
        'package tests\nimport "os"\nfunc f() { _ = os.Getenv("IGNORED_IN_TESTS_DIR"); }\n',
        encoding="utf-8",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_specimen_financial_advisor_parses_clean(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib" / "go" / "financial-advisor"
    if not specimen.exists():
        specimen = repo_root / "go" / "agents" / "financial-advisor"

    assert specimen.exists(), f"Specimen not found at {specimen}"
    assert _run(specimen, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_a_file_that_does_not_parse_is_reported_not_skipped(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=x\n",
        broken="package main\nfunc oops( {\n",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not valid Go" in out
    assert "[PASS]" not in out
    assert f"::error file={tmp_path / 'broken.go'}::" in out


def test_non_utf8_env_example_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "MY_VAR=caf\u00e9\n".encode("latin-1"),
        main='package main\nimport "os"\nfunc main() { _ = os.Getenv("MY_VAR") }\n',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out
    assert out.count("::error file=") == 1


def test_non_utf8_go_source_is_reported(tmp_path, monkeypatch, capsys):
    target = tmp_path / ".env.example"
    target.write_text("MY_VAR=value\n", encoding="utf-8")
    go_path = tmp_path / "bad.go"
    go_path.write_bytes("package main\n// caf\xe9\n".encode("latin-1"))
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be decoded as UTF-8" in out
    assert f"::error file={go_path}::" in out


def test_missing_env_example_is_not_this_checkers_failure(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        None,
        main='package main\nimport "os"\nfunc main() { _ = os.Getenv("X") }\n',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_a_path_that_is_not_a_directory_is_a_ci_fault(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_go.py", str(tmp_path / "nope")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_a_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_env_vars_go.py"])
    assert m.main() == EXIT_CI_FAULT
    assert "[ci-fault]" in capsys.readouterr().out


def test_an_unexpected_crash_is_a_ci_fault_not_the_contributors_fault(
    tmp_path, monkeypatch, capsys
):
    def boom(*_args, **_kwargs):
        raise RuntimeError("checker bug")

    monkeypatch.setattr(m, "_collect_used_vars", boom)
    _recipe(tmp_path, "A=1\n")
    assert _run(tmp_path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "RuntimeError: checker bug" in out
    assert "::error file=" not in out
