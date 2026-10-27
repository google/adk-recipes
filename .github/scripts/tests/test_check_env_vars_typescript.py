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
"""Unit tests for check_env_vars_typescript.py.

Verifies that TypeScript recipe environment variable reads (process.env,
import.meta.env, destructuring, ?? and || defaults) are validated against
.env.example:
  - Declared variables pass (exit 0).
  - Allowlisted variables (HOME, PATH, CI, NODE_ENV, GITHUB_*, etc.) are not reported (exit 0).
  - Undeclared variables are reported with file, line, and fix (exit 1).
  - Nullish coalescing (??) and logical OR (||) default values are surfaced.
  - Object destructuring patterns are detected.
  - import.meta.env reads are detected.
  - TSX/JSX syntax is supported.
  - Test files (*.test.ts, *.spec.ts, *.d.ts) and test directories are ignored.
  - The financial-advisor TypeScript specimen in the repository passes clean (exit 0).
  - Missing .env.example exits 0 (owned by a separate required-files check).
  - Non-UTF8 files and syntax errors are reported (exit 1).
  - CI faults (bad CLI arguments, missing paths, crashes) exit 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_env_vars_typescript as m

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
        ext = ".tsx" if name.endswith("_tsx") else ".ts"
        clean_name = name[:-4] if name.endswith("_tsx") else name
        path = tmp_path / f"{clean_name}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path | str, monkeypatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_typescript.py", str(tmp_path)]
    )
    return m.main()


def test_declared_variable_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "const project = process.env.GOOGLE_CLOUD_PROJECT;\n"
            "export { project };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_subscript_and_template_literals_pass(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "PORT=8080\nHOST=localhost\nAPI_KEY=secret\n",
        main=(
            'const port = process.env["PORT"];\n'
            "const host = process.env['HOST'];\n"
            "const key = process.env[`API_KEY`];\n"
            "export { port, host, key };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_allowlisted_variable_is_not_reported(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "GOOGLE_CLOUD_PROJECT=my-project\n",
        main=(
            "const home = process.env.HOME;\n"
            "const path = process.env.PATH;\n"
            "const ci = process.env.CI;\n"
            "const nodeEnv = process.env.NODE_ENV;\n"
            "const token = process.env.GITHUB_TOKEN;\n"
            "const runner = process.env.RUNNER_OS;\n"
            "export { home, path, ci, nodeEnv, token, runner };\n"
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
            "// Header comment\n"
            "const project = process.env.GOOGLE_CLOUD_PROJECT;\n"
            "\n"
            "\n"
            "const model =\n"
            "    process.env.MODEL_NAME;\n"
            "export { project, model };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MODEL_NAME" in out
    # The read starts on line 6
    assert f"{tmp_path / 'main.ts'}:6" in out
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
            "const one = process.env.ONE;\n"
            'const two = process.env["TWO"];\n'
            "export { one, two };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    assert capsys.readouterr().out.count("::error file=") == 2


def test_nullish_coalescing_default_is_surfaced(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            'const model = process.env.MODEL_NAME ?? "gemini-3.5-flash";\n'
            "export { model };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MODEL_NAME" in out
    assert "gemini-3.5-flash" in out
    assert "MODEL_NAME=gemini-3.5-flash" in out


def test_upgrade_line_number_when_default_found_later(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            "const a = process.env.MODEL_NAME;\n"
            "\n"
            'const b = process.env.MODEL_NAME ?? "gemini-3.5-flash";\n'
            "export { a, b };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "MODEL_NAME" in out
    assert f"{tmp_path / 'main.ts'}:3" in out
    assert "MODEL_NAME=gemini-3.5-flash" in out


def test_logical_or_default_is_surfaced(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "# nothing declared\n",
        main=(
            "const timeout = process.env.TIMEOUT_MS || 5000;\n"
            "export { timeout };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "TIMEOUT_MS" in out
    assert "5000" in out
    assert "TIMEOUT_MS=5000" in out


def test_destructuring_process_env(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=val\n",
        main=(
            "const {\n"
            "    DECLARED_VAR,\n"
            '    UNDECLARED_ONE = "default_one",\n'
            "    UNDECLARED_TWO: aliasTwo,\n"
            '    UNDECLARED_THREE: aliasThree = "default_three",\n'
            "} = process.env;\n"
            "export { DECLARED_VAR, UNDECLARED_ONE, aliasTwo, aliasThree };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "DECLARED_VAR" not in out
    assert "UNDECLARED_ONE" in out
    assert "UNDECLARED_ONE=default_one" in out
    assert "UNDECLARED_TWO" in out
    assert f"UNDECLARED_TWO={m.PLACEHOLDER}" in out
    assert "UNDECLARED_THREE" in out
    assert "UNDECLARED_THREE=default_three" in out


def test_import_meta_env_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_API=url\n",
        main=(
            "const url = import.meta.env.DECLARED_API;\n"
            "const secret = import.meta.env.VITE_SECRET;\n"
            'const key = import.meta.env["VITE_KEY"];\n'
            "const { VITE_DEST } = import.meta.env;\n"
            "export { url, secret, key, VITE_DEST };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "DECLARED_API" not in out
    assert "VITE_SECRET" in out
    assert "VITE_KEY" in out
    assert "VITE_DEST" in out


def test_optional_chaining_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "# nothing\n",
        main=(
            "const a = process?.env?.OPTIONAL_VAR;\n"
            "const b = import.meta?.env?.OPTIONAL_META;\n"
            "export { a, b };\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "OPTIONAL_VAR" in out
    assert "OPTIONAL_META" in out


def test_tsx_jsx_files_detected(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "# nothing\n",
        component_tsx=(
            "import React from 'react';\n"
            "export const App = () => (\n"
            "    <div>\n"
            "        <h1>{process.env.APP_TITLE ?? 'My App'}</h1>\n"
            "        <p>{process.env.REACT_APP_SECRET}</p>\n"
            "    </div>\n"
            ");\n"
        ),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "APP_TITLE" in out
    assert "APP_TITLE=My App" in out
    assert "REACT_APP_SECRET" in out


def test_test_files_and_directories_are_ignored(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        "DECLARED_VAR=val\n",
        main=("const val = process.env.DECLARED_VAR;\nexport { val };\n"),
        agent_test=(
            "import { test } from 'vitest';\n"
            "test('something', () => {\n"
            "    const t = process.env.TEST_ONLY_SECRET;\n"
            "});\n"
        ),
        runnability_spec=(
            "import { describe } from 'vitest';\n"
            "describe('runnability', () => {\n"
            "    const r = process.env.RUNNABILITY_ONLY_SECRET;\n"
            "});\n"
        ),
    )
    # Also write a file in tests/ subdirectory
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "helper.ts").write_text(
        "export const h = process.env.IGNORED_IN_TESTS_DIR;\n",
        encoding="utf-8",
    )
    # Also write a .d.ts declaration file
    (tmp_path / "types.d.ts").write_text(
        "declare const x: typeof process.env.IGNORED_TYPE_VAR;\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_specimen_financial_advisor_parses_clean(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib" / "typescript" / "financial-advisor"

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
        broken="const oops = (;\n",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not valid TypeScript" in out
    assert "[PASS]" not in out
    assert f"::error file={tmp_path / 'broken.ts'}::" in out


def test_non_utf8_env_example_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        "MY_VAR=caf\u00e9\n".encode("latin-1"),
        main="const v = process.env.MY_VAR;\n",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out
    assert out.count("::error file=") == 1


def test_non_utf8_typescript_source_is_reported(tmp_path, monkeypatch, capsys):
    target = tmp_path / ".env.example"
    target.write_text("MY_VAR=value\n", encoding="utf-8")
    ts_path = tmp_path / "bad.ts"
    ts_path.write_bytes("// caf\xe9\n".encode("latin-1"))
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "could not be decoded as UTF-8" in out
    assert f"::error file={ts_path}::" in out


def test_missing_env_example_is_not_this_checkers_failure(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        None,
        main="const v = process.env.X;\n",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_a_path_that_is_not_a_directory_is_a_ci_fault(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["check_env_vars_typescript.py", str(tmp_path / "nope")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_a_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_env_vars_typescript.py"])
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
