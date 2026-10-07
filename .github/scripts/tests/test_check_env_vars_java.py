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
System.getenv().get(), System.getProperty, static imports, Dotenv) are
validated against .env.example:
  - Declared variables pass (exit 0).
  - System.getenv().get() and getOrDefault() pass when declared (exit 0).
  - System.getProperty("VAR") and System.getProperty("VAR", "default") pass when declared (exit 0).
  - Allowlisted variables (HOME, PATH, CI, GITHUB_*, etc.) are not reported (exit 0).
  - Undeclared variables are reported with file, line, and fix (exit 1).
  - Undeclared System.getProperty notes that it is a system property.
  - Text blocks and multiline calls are supported.
  - Constant variable references are resolved.
  - Comments and plain string literals mentioning System.getenv are ignored.
  - Test files (src/test/*, tests/*) are ignored.
  - Specimen Java recipes in the repository pass clean (exit 0).
  - Missing .env.example exits 0 (owned by a separate required-files check).
  - Non-UTF-8 files are reported (exit 1).
  - CI faults (bad CLI arguments, missing paths, crashes) exit 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_env_vars_java as m
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
        # Support names with forward slashes for subdirectories, e.g. "src/main/java/Main"
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
            "    public static void main(String[] args) {\n"
            '        String p = System.getenv("GOOGLE_CLOUD_PROJECT");\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_system_getenv_get_map_method_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "PORT=8080\nAPI_KEY=secret\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String port = System.getenv().get("PORT");\n'
            '        String key = System.getenv().getOrDefault("API_KEY", "default");\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_system_getproperty_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "CONFIG_PATH=/etc/config\nAPP_MODE=prod\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String path = System.getProperty("CONFIG_PATH");\n'
            '        String mode = System.getProperty("APP_MODE", "dev");\n'
            "    }\n"
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
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String home = System.getenv("HOME");\n'
            '        String path = System.getenv("PATH");\n'
            '        String ci = System.getenv("CI");\n'
            '        String gh = System.getenv("GITHUB_TOKEN");\n'
            '        String it = System.getenv("INTEGRATION_TEST");\n'
            '        String p = System.getenv("GOOGLE_CLOUD_PROJECT");\n'
            "    }\n"
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
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String p = System.getenv("GOOGLE_CLOUD_PROJECT");\n'
            "        String model = System.getenv(\n"
            '            "MODEL_NAME"\n'
            "        );\n"
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MODEL_NAME" in out
    # The read starts on line 6
    assert f"{tmp_path / 'main.java'}:6" in out
    # A literal line to paste
    assert f"MODEL_NAME={m.PLACEHOLDER}" in out
    assert f"::error file={tmp_path / '.env.example'}::" in out


def test_undeclared_property_notes_system_property(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String prop = System.getProperty("APP_CONFIG");\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "APP_CONFIG" in out
    assert "system property" in out


def test_each_undeclared_variable_gets_its_own_annotation(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String a = System.getenv("ONE");\n'
            '        String b = System.getProperty("TWO");\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    assert capsys.readouterr().out.count("::error file=") == 2


def test_static_import_getenv_and_getproperty_detected(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "DECLARED_VAR=val\n",
        main=(
            "package com.example;\n\n"
            "import static java.lang.System.getenv;\n"
            "import static java.lang.System.getProperty;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String a = getenv("DECLARED_VAR");\n'
            '        String b = getenv("UNDECLARED_STATIC_ENV");\n'
            '        String c = getProperty("UNDECLARED_STATIC_PROP");\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_STATIC_ENV" in out
    assert "UNDECLARED_STATIC_PROP" in out
    assert "DECLARED_VAR" not in out


def test_text_block_multiline_string_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=val\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        String a = System.getenv("""\n'
            "            DECLARED_VAR\n"
            '            """);\n'
            '        String b = System.getenv("""\n'
            "            UNDECLARED_RAW_VAR\n"
            '            """);\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_RAW_VAR" in out
    assert "DECLARED_VAR" not in out


def test_constant_variable_reference_resolved(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "CONST_DECLARED=val\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            '    private static final String KEY1 = "CONST_DECLARED";\n'
            '    private static final String KEY2 = "MISSING_VAR";\n\n'
            "    public static void main(String[] args) {\n"
            "        String a = System.getenv(KEY1);\n"
            "        String b = System.getenv(KEY2);\n"
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MISSING_VAR" in out
    assert "CONST_DECLARED" not in out


def test_comments_and_plain_strings_are_not_detected(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "DECLARED_VAR=value\n",
        main=(
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            '        // String x = System.getenv("IGNORED_LINE_COMMENT");\n'
            '        /* String y = System.getenv("IGNORED_BLOCK_COMMENT"); */\n'
            '        String docs = "Check System.getenv(\\"IGNORED_STRING\\") docs";\n'
            '        String ok = System.getenv("DECLARED_VAR");\n'
            "    }\n"
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
            "package com.example;\n\n"
            "public class Main {\n"
            "    public static void lookup(String key) {\n"
            "        String v = System.getenv(key);\n"
            '        String d = System.getenv("DECLARED_VAR");\n'
            "    }\n"
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
            "src/main/java/Main": (
                "package com.example;\n\n"
                "public class Main {\n"
                "    public static void main(String[] args) {\n"
                '        String x = System.getenv("DECLARED_VAR");\n'
                "    }\n"
                "}\n"
            ),
            "src/test/java/RunnabilityTest": (
                "package com.example;\n\n"
                "public class RunnabilityTest {\n"
                "    public void testRunnability() {\n"
                '        String x = System.getenv("TEST_ONLY_SECRET");\n'
                "    }\n"
                "}\n"
            ),
            "tests/Helper": (
                "package tests;\n\n"
                "public class Helper {\n"
                "    public void helper() {\n"
                '        String x = System.getenv("IGNORED_IN_TESTS_DIR");\n'
                "    }\n"
                "}\n"
            ),
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_dotenv_and_helper_functions_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "MODEL_NAME=gemini-3.5-flash\nDB_HOST=localhost\n",
        main=(
            "package com.example;\n\n"
            "import io.github.cdimascio.dotenv.Dotenv;\n\n"
            "public class Main {\n"
            "    private static final Dotenv DOTENV = Dotenv.load();\n\n"
            "    private static String env(String key) {\n"
            "        return DOTENV.get(key);\n"
            "    }\n\n"
            "    public static void main(String[] args) {\n"
            '        String model = env("MODEL_NAME");\n'
            '        String host = DOTENV.get("DB_HOST");\n'
            '        String missing1 = env("UNDECLARED_HELPER_ENV");\n'
            '        String missing2 = DOTENV.get("UNDECLARED_DOTENV");\n'
            "    }\n"
            "}\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "UNDECLARED_HELPER_ENV" in out
    assert "UNDECLARED_DOTENV" in out
    assert "MODEL_NAME" not in out
    assert "DB_HOST" not in out


def test_a_file_that_does_not_parse_is_reported_not_skipped(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=x\n",
        broken="package com.example;\npublic class Broken { public void oops( {\n",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not valid Java" in out
    assert "[PASS]" not in out
    assert f"::error file={tmp_path / 'broken.java'}::" in out


def test_non_utf8_env_example_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "MY_VAR=caf\u00e9\n".encode("latin-1"),
        main='package com.example;\npublic class Main { public static void main(String[] args) { System.getenv("MY_VAR"); } }\n',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out
    assert out.count("::error file=") == 1


def test_non_utf8_java_source_is_reported(tmp_path, monkeypatch, capsys):
    target = tmp_path / ".env.example"
    target.write_text("MY_VAR=value\n", encoding="utf-8")
    java_path = tmp_path / "bad.java"
    java_path.write_bytes(
        "package com.example;\n// caf\xe9\n".encode("latin-1")
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be decoded as UTF-8" in out
    assert f"::error file={java_path}::" in out


def test_missing_env_example_is_not_this_checkers_failure(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        None,
        main='package com.example;\npublic class Main { public static void main(String[] args) { System.getenv("X"); } }\n',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_a_path_that_is_not_a_directory_is_a_ci_fault(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_java.py", str(tmp_path / "nope")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_a_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_env_vars_java.py"])
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


@pytest.mark.parametrize(
    "recipe_name",
    [
        "financial-advisor",
        "time-series-forecasting",
    ],
)
def test_specimen_java_recipes_parse_clean(recipe_name, monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib" / "java" / recipe_name
    if not specimen.is_dir():
        specimen = repo_root / "core" / "java" / recipe_name
    if not specimen.is_dir():
        pytest.skip(f"{recipe_name} not present in this workspace")

    assert _run(specimen, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out
    assert "5 detected, 0 in the OS allowlist, 5 declared" in out
