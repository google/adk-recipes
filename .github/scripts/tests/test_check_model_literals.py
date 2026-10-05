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
"""Unit tests for check_model_literals.py.

Verifies model literal detection across all supported languages (Python, Go,
Java, Kotlin, TypeScript), per-language test exclusions, and the CI exit-code
contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_model_literals as m

# Exit codes from contract
EXIT_OK = 0
EXIT_CI_FAULT = 2


def _write_files(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel_path_str, content in files.items():
        file_path = tmp_path / rel_path_str
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_model_literals.py", str(tmp_path)])
    return m.main()


def test_no_model_literals_passes(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_files(
        tmp_path,
        {
            "app/agent.py": 'model = os.getenv("MODEL_NAME")\n',
            "main.go": 'model := os.Getenv("MODEL_NAME")\n',
            "src/main/java/App.java": 'String m = System.getenv("MODEL");\n',
            "src/main/kotlin/App.kt": 'val m = System.getenv("MODEL")\n',
            "src/index.ts": 'const m = process.env["MODEL"];\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "::notice" not in out
    assert "[NOTICE]" not in out


def test_python_model_literal_emits_exact_notice_and_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    py_file = tmp_path / "app" / "agent.py"
    _write_files(
        tmp_path,
        {
            "app/agent.py": 'model = "gemini-1.5-flash"\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    expected_annotation = (
        f"::notice file={py_file},line=1::Possible hardcoded model name "
        f'"gemini-1.5-flash" detected. If this is a model identifier, '
        f"consider using an environment variable instead."
    )
    expected_human = (
        f"[NOTICE] {py_file}:1 \u2014 possible hardcoded model name: "
        f'"gemini-1.5-flash"'
    )
    assert expected_annotation in out
    assert expected_human in out


def test_python_test_exclusion(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_files(
        tmp_path,
        {
            "tests/test_agent.py": 'model = "gemini-1.5-flash"\n',
            "app/tests/test_sub.py": 'model = "claude-3-5-sonnet"\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "::notice" not in out
    assert "[NOTICE]" not in out


def test_go_model_literal_and_test_exclusion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    go_main = tmp_path / "main.go"
    _write_files(
        tmp_path,
        {
            "main.go": 'model := "claude-3-5-sonnet"\n',
            "main_test.go": 'model := "claude-3-5-sonnet"\n',
            "pkg/util_test.go": 'model := "gemini-2.0-flash"\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert f"::notice file={go_main},line=1::" in out
    assert "[NOTICE] " in out
    assert "main_test.go" not in out
    assert "util_test.go" not in out


def test_java_model_literal_and_test_exclusion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    java_file = (
        tmp_path / "src" / "main" / "java" / "com" / "example" / "Agent.java"
    )
    _write_files(
        tmp_path,
        {
            "src/main/java/com/example/Agent.java": 'String m = "llama-3-70b";\n',
            "src/test/java/com/example/AgentTest.java": 'String m = "llama-3-70b";\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert f"::notice file={java_file},line=1::" in out
    assert "AgentTest.java" not in out


def test_kotlin_model_literal_and_test_exclusion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    kt_file = tmp_path / "src" / "main" / "kotlin" / "Agent.kt"
    kts_file = tmp_path / "build.gradle.kts"
    _write_files(
        tmp_path,
        {
            "src/main/kotlin/Agent.kt": 'val m = "mistral-large"\n',
            "build.gradle.kts": 'val m = "grok-1"\n',
            "src/test/kotlin/AgentTest.kt": 'val m = "mistral-large"\n',
            "src/test/kotlin/BuildTest.kts": 'val m = "grok-1"\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert f"::notice file={kt_file},line=1::" in out
    assert f"::notice file={kts_file},line=1::" in out
    assert "AgentTest.kt" not in out
    assert "BuildTest.kts" not in out


def test_typescript_and_tsx_model_literal_and_test_exclusion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ts_file = tmp_path / "src" / "agent.ts"
    tsx_file = tmp_path / "src" / "App.tsx"
    _write_files(
        tmp_path,
        {
            "src/agent.ts": 'const m = "codestral-latest";\n',
            "src/App.tsx": 'const m = "imagen-3.0";\n',
            "src/agent.test.ts": 'const m = "codestral-latest";\n',
            "src/agent.spec.ts": 'const m = "codestral-latest";\n',
            "src/App.test.tsx": 'const m = "imagen-3.0";\n',
            "src/App.spec.tsx": 'const m = "imagen-3.0";\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert f"::notice file={ts_file},line=1::" in out
    assert f"::notice file={tsx_file},line=1::" in out
    assert ".test.ts" not in out
    assert ".spec.ts" not in out
    assert ".test.tsx" not in out
    assert ".spec.tsx" not in out


def test_all_model_prefixes(tmp_path: Path, monkeypatch, capsys) -> None:
    prefixes = [
        "gemini-",
        "gemini-exp-",
        "imagen-",
        "claude-",
        "llama-",
        "meta/llama-",
        "mistral-",
        "codestral-",
        "phi-",
        "grok-",
        "command-",
        "jamba-",
    ]
    lines = [
        f'm{i} = "{prefix}test-model"' for i, prefix in enumerate(prefixes)
    ]
    _write_files(tmp_path, {"models.py": "\n".join(lines) + "\n"})

    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    for prefix in prefixes:
        assert f'"{prefix}test-model"' in out
    assert out.count("::notice") == len(prefixes)


def test_single_and_double_quotes(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_files(
        tmp_path,
        {
            "models.py": (
                "m1 = \"gemini-1.5-flash\"\nm2 = 'claude-3-5-sonnet'\n"
            )
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert 'Possible hardcoded model name "gemini-1.5-flash"' in out
    assert "Possible hardcoded model name 'claude-3-5-sonnet'" in out


def test_mismatched_quotes_do_not_match(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _write_files(
        tmp_path,
        {
            "models.py": (
                "m1 = 'gemini-1.5-flash\"\nm2 = \"claude-3-5-sonnet'\n"
            )
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "::notice" not in out
    assert "[NOTICE]" not in out


def test_excluded_directories_are_skipped(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _write_files(
        tmp_path,
        {
            ".venv/lib/site.py": 'm = "gemini-1.5-flash"\n',
            "node_modules/pkg/index.ts": 'm = "gemini-1.5-flash"\n',
            ".git/hooks/pre-commit.py": 'm = "gemini-1.5-flash"\n',
            "build/generated/Gen.java": 'm = "gemini-1.5-flash"\n',
            "target/Gen.java": 'm = "gemini-1.5-flash"\n',
            "__pycache__/foo.py": 'm = "gemini-1.5-flash"\n',
        },
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "::notice" not in out
    assert "[NOTICE]" not in out


def test_non_existent_directory_is_ci_fault(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["check_model_literals.py", str(tmp_path / "nonexistent")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error::" in out
    assert "not a directory" in out


def test_invalid_argument_count_is_ci_fault(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["check_model_literals.py", str(tmp_path), "extra"]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error::" in out
    assert "expected exactly one recipe directory" in out


def test_unexpected_crash_is_ci_fault(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash in checker")

    monkeypatch.setattr(m, "_run", boom)
    _write_files(tmp_path, {"agent.py": "x = 1\n"})
    assert _run(tmp_path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "RuntimeError: simulated crash in checker" in out
