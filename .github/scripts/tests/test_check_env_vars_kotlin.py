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
"""Unit tests for check_env_vars_kotlin.py.

Verifies that Kotlin recipe environment variable reads (System.getenv and
System.getProperty) are validated against .env.example:
  - Declared variables pass (exit 0).
  - Elvis default reads (System.getenv("VAR") ?: "default") pass when declared (exit 0).
  - System.getProperty("VAR") and System.getProperty("VAR", "default") pass when declared (exit 0).
  - Allowlisted variables (HOME, PATH, CI, GITHUB_*, etc.) are not reported (exit 0).
  - Undeclared variables are reported with file, line, and fix (exit 1).
  - String interpolation and multiline/triple-quoted calls are supported.
  - Comments and plain string literals mentioning System.getenv are ignored.
  - Test files (src/test/*, tests/*) are ignored.
  - The llm-auditor Kotlin specimen in the repository passes clean (exit 0).
  - Missing .env.example exits 0 (owned by a separate required-files check).
  - Non-UTF8 files are reported (exit 1).
  - CI faults (bad CLI arguments, missing paths, crashes) exit 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_env_vars_kotlin as m
import pytest

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
        # Support names with forward slashes for subdirectories, e.g. "src/main/kotlin/Main"
        path = tmp_path / f"{name}.kt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path | str, monkeypatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_kotlin.py", str(tmp_path)]
    )
    return m.main()


def test_declared_variable_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val p = System.getenv("GOOGLE_CLOUD_PROJECT")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_elvis_default_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "PORT=8080\nAPI_KEY=secret\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val port = System.getenv("PORT") ?: "8080"\n'
            '    val key = System.getenv("API_KEY") ?: error("API_KEY required")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_get_property_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "CONFIG_PATH=/etc/config\nAPP_MODE=prod\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val path = System.getProperty("CONFIG_PATH")\n'
            '    val mode = System.getProperty("APP_MODE", "dev")\n'
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
            "package com.example\n\n"
            "fun main() {\n"
            '    val home = System.getenv("HOME")\n'
            '    val path = System.getenv("PATH")\n'
            '    val ci = System.getenv("CI")\n'
            '    val gh = System.getenv("GITHUB_TOKEN")\n'
            '    val it = System.getenv("INTEGRATION_TEST")\n'
            '    val p = System.getenv("GOOGLE_CLOUD_PROJECT")\n'
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
            "package com.example\n\n"
            "fun main() {\n"
            '    val p = System.getenv("GOOGLE_CLOUD_PROJECT")\n'
            "    val model = System.getenv(\n"
            '        "MODEL_NAME",\n'
            "    )\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MODEL_NAME" in out
    # The read starts on line 5
    assert f"{tmp_path / 'main.kt'}:5" in out
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
            "package com.example\n\n"
            "fun main() {\n"
            '    val a = System.getenv("ONE")\n'
            '    val b = System.getProperty("TWO")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    assert capsys.readouterr().out.count("::error file=") == 2


def test_triple_quoted_string_literal_is_detected(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val a = System.getenv("""DECLARED_VAR""")\n'
            '    val b = System.getenv("""UNDECLARED_RAW_VAR""")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_RAW_VAR" in out
    assert "DECLARED_VAR" not in out


def test_string_interpolation_is_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val msg = "Connected to ${System.getenv("DATABASE_URL")}"\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "DATABASE_URL" in out
    assert f"{tmp_path / 'main.kt'}:4" in out


def test_comments_and_string_literals_are_not_detected(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    // val x = System.getenv("IGNORED_LINE_COMMENT")\n'
            '    /* val y = System.getenv("IGNORED_BLOCK_COMMENT") */\n'
            "    /*\n"
            '     * Nested: /* val z = System.getenv("IGNORED_NESTED") */\n'
            "     */\n"
            '    val docs = "Check System.getenv(\\"IGNORED_STRING\\") docs"\n'
            '    val ok = System.getenv("DECLARED_VAR")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_dynamic_expression_is_not_matched(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package com.example\n\n"
            "fun lookup(key: String) {\n"
            "    val v = System.getenv(key)\n"
            '    val d = System.getenv("DECLARED_VAR")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_test_files_and_directories_are_ignored(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=val\n",
        **{
            "src/main/kotlin/Main": (
                "package com.example\n\n"
                "fun main() {\n"
                '    val x = System.getenv("DECLARED_VAR")\n'
                "}\n"
            ),
            "src/test/kotlin/RunnabilityTest": (
                "package com.example\n\n"
                "fun testRunnability() {\n"
                '    val x = System.getenv("TEST_ONLY_SECRET")\n'
                "}\n"
            ),
            "tests/Helper": (
                "package tests\n\n"
                "fun helper() {\n"
                '    val x = System.getenv("IGNORED_IN_TESTS_DIR")\n'
                "}\n"
            ),
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_env_helper_function_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "MODEL_NAME=gemini-3.5-flash\n",
        main=(
            "package com.example\n\n"
            "private fun env(key: String): String? = System.getenv(key)\n\n"
            "fun main() {\n"
            '    val model = env("MODEL_NAME")\n'
            '    val missing = env("UNDECLARED_CONFIG")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_CONFIG" in out
    assert "MODEL_NAME" not in out


def test_dotenv_map_and_methods_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DB_HOST=localhost\nDB_PORT=5432\nCACHE_KEY=abc\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val host = dotenv["DB_HOST"]\n'
            '    val port = dotenv.get("DB_PORT")\n'
            '    val key = Dotenv.load()["CACHE_KEY"]\n'
            '    val secret = dotenv["UNDECLARED_SECRET"]\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_SECRET" in out
    assert "DB_HOST" not in out


def test_system_getenv_map_indexing_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "APP_KEY=secret\n",
        main=(
            "package com.example\n\n"
            "fun main() {\n"
            '    val key = System.getenv()["APP_KEY"]\n'
            '    val missing = System.getenv().get("MISSING_KEY")\n'
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MISSING_KEY" in out
    assert "APP_KEY" not in out


def test_specimen_llm_auditor_parses_clean(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "core" / "kotlin" / "llm-auditor"
    if not specimen.is_dir():
        specimen = repo_root / "kotlin" / "agents" / "llm-auditor"
    if not specimen.is_dir():
        pytest.skip("core/kotlin/llm-auditor not present in this workspace")

    assert _run(specimen, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_specimen_financial_advisor_parses_clean(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib" / "kotlin" / "financial-advisor"
    if not specimen.is_dir():
        specimen = repo_root / "kotlin" / "agents" / "financial-advisor"
    if not specimen.is_dir():
        pytest.skip(
            "contrib/kotlin/financial-advisor not present in this workspace"
        )

    assert _run(specimen, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out
    assert "7 detected, 2 in the OS allowlist, 5 declared" in out


def test_non_utf8_env_example_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "MY_VAR=caf\u00e9\n".encode("latin-1"),
        main='package com.example\nfun main() { System.getenv("MY_VAR") }\n',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out
    assert out.count("::error file=") == 1


def test_non_utf8_kotlin_source_is_reported(tmp_path, monkeypatch, capsys):
    target = tmp_path / ".env.example"
    target.write_text("MY_VAR=value\n", encoding="utf-8")
    kt_path = tmp_path / "bad.kt"
    kt_path.write_bytes("package com.example\n// caf\xe9\n".encode("latin-1"))
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be decoded as UTF-8" in out
    assert f"::error file={kt_path}::" in out


def test_missing_env_example_is_not_this_checker_failure(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        None,
        main='package com.example\nfun main() { System.getenv("X") }\n',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_a_path_that_is_not_a_directory_is_a_ci_fault(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_kotlin.py", str(tmp_path / "nope")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_a_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_env_vars_kotlin.py"])
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
