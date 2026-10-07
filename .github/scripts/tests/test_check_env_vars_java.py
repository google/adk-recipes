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
"""Unit tests for check_env_vars_java.py.

Verifies that Java recipe environment variable reads (System.getenv,
System.getProperty, helper methods, Dotenv) are validated against .env.example:
  - Declared variables pass (exit 0).
  - System.getenv().get("VAR") passes when declared (exit 0).
  - System.getProperty("VAR") and System.getProperty("VAR", "default") pass when declared (exit 0).
  - env("VAR") and requireEnv("VAR") pass when declared (exit 0).
  - Dotenv.get("VAR") passes when declared (exit 0).
  - Allowlisted variables (HOME, PATH, CI, GITHUB_*, etc.) are not reported (exit 0).
  - Undeclared variables are reported with file, line, and fix (exit 1).
  - Comments and plain string literals mentioning System.getenv are ignored.
  - Test files (src/test/*, tests/*, *Test.java) are ignored.
  - The frozen specimens in the repository pass clean (exit 0).
  - Missing .env.example exits 0 (owned by a separate required-files check).
  - Non-UTF8 files are reported (exit 1).
  - CI faults (bad CLI arguments, missing paths, crashes) exit 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_env_vars_java as m

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
        path = tmp_path / f"{name}.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path | str, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_env_vars_java.py", str(tmp_path)])
    return m.main()


def test_declared_variable_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String p = System.getenv("GOOGLE_CLOUD_PROJECT");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_getenv_map_get_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "API_KEY=secret\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String key = System.getenv().get("API_KEY");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_get_property_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "CONFIG_PATH=/tmp/config\nSERVER_PORT=8080\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String cfg = System.getProperty("CONFIG_PATH");\n'
            '    String port = System.getProperty("SERVER_PORT", "8080");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_env_helper_functions_pass(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "MODEL_NAME=gemini-3.7-flash\nLOCATION=us-central1\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String m = requireEnv("MODEL_NAME");\n'
            '    String loc = env("LOCATION");\n'
            "  }\n"
            "  private static String requireEnv(String k) { return env(k); }\n"
            "  private static String env(String k) { return System.getenv(k); }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_dotenv_calls_pass(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DB_PASS=secret\n",
        main=(
            "package com.example;\n"
            "import io.github.cdimascio.dotenv.Dotenv;\n\n"
            "public class Main {\n"
            "  private static final Dotenv DOTENV = Dotenv.load();\n"
            "  public static void main(String[] args) {\n"
            '    String pass = DOTENV.get("DB_PASS");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_allowlist_variables_skipped(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String home = System.getenv("HOME");\n'
            '    String path = System.getenv("PATH");\n'
            '    String ci = System.getenv("CI");\n'
            '    String port = System.getenv("PORT");\n'
            '    String gh = System.getenv("GITHUB_TOKEN");\n'
            '    String integration = System.getenv("INTEGRATION_TEST");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_undeclared_variable_reported(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String secret = System.getenv("MISSING_SECRET");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "MISSING_SECRET" in out
    assert f"{tmp_path / 'main.java'}:5" in out


def test_undeclared_property_reported(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String custom = System.getProperty("CUSTOM_PROP");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "CUSTOM_PROP" in out
    assert "System.getProperty" in out


def test_comments_and_strings_ignored(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    // String secret = System.getenv("COMMENTED_SECRET");\n'
            '    /* String secret2 = System.getenv("BLOCK_SECRET"); */\n'
            '    String doc = "Use System.getenv(\\"STRING_SECRET\\") to configure";\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_test_files_ignored(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "",
        **{
            "src/main/java/Main": (
                "package com.example;\n\n"
                "public class Main {\n"
                "  public static void main(String[] args) {}\n"
                "}\n"
            ),
            "src/test/java/MainTest": (
                "package com.example;\n\n"
                "public class MainTest {\n"
                "  public void test() {\n"
                '    String testKey = System.getenv("TEST_SPECIFIC_KEY");\n'
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_missing_env_example_skipped(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        None,
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "  public static void main(String[] args) {\n"
            '    String secret = System.getenv("ANY_VAR");\n'
            "  }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "[SKIP]" in capsys.readouterr().out


def test_non_utf8_env_example_fails(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        b"\xff\xfe\x00\x00SOME_VAR=foo",
        main="public class Main {}",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "not valid UTF-8" in out


def test_non_utf8_source_fails(tmp_path, monkeypatch, capsys):
    target = tmp_path / "Main.java"
    target.write_bytes(b"\xff\xfe\x00\x00invalid")
    (tmp_path / ".env.example").write_text("VAR=1\n", encoding="utf-8")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "could not be decoded as UTF-8" in out


def test_live_time_series_forecasting_specimen(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib/java/time-series-forecasting"
    if specimen.is_dir():
        assert _run(specimen, monkeypatch) == EXIT_OK
        assert "::error" not in capsys.readouterr().out


def test_live_financial_advisor_specimen(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib/java/financial-advisor"
    if specimen.is_dir():
        assert _run(specimen, monkeypatch) == EXIT_OK
        assert "::error" not in capsys.readouterr().out


def test_no_args_exits_ci_fault(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_env_vars_java.py"])
    assert m.main() == EXIT_CI_FAULT


def test_too_many_args_exits_ci_fault(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_env_vars_java.py", "arg1", "arg2"])
    assert m.main() == EXIT_CI_FAULT


def test_non_directory_arg_exits_ci_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_java.py", str(tmp_path / "nonexistent")]
    )
    assert m.main() == EXIT_CI_FAULT
