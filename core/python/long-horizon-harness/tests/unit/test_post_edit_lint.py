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

"""Deterministic tests for the post-edit ruff diagnostics helper.

The helper shells ``ruff check --output-format=json`` through the env interface.
We stub the environment's ``execute`` to return canned ruff JSON so the test
never depends on a real ruff invocation or on LLM behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from horizon.environment_context import set_active_environment

pytestmark = pytest.mark.asyncio


@dataclass
class _Result:
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False


class _StubEnv:
    """Minimal BaseEnvironment stand-in capturing the executed command."""

    on_host_fs = True

    def __init__(self, result: _Result | Exception) -> None:
        self._result = result
        self.commands: list[str] = []

    async def execute(self, command, *, timeout=None, env=None):
        self.commands.append(command)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _ruff_json(path: str, n: int) -> str:
    return json.dumps(
        [
            {
                "filename": path,
                "location": {"row": i + 1, "column": 1},
                "code": "F821",
                "message": f"undefined name x{i}",
            }
            for i in range(n)
        ]
    )


class TestPostEditDiagnostics:
    async def test_returns_block_for_ruff_findings(self) -> None:
        from horizon.tools.file_ops import _post_edit_diagnostics

        path = "/work/code.py"
        env = _StubEnv(_Result(exit_code=1, stdout=_ruff_json(path, 2)))
        set_active_environment(env)

        block = await _post_edit_diagnostics(path)

        assert block is not None
        assert "undefined name x0" in block
        assert "F821" in block
        assert any("ruff check" in c for c in env.commands)

    async def test_caps_findings_at_twenty(self) -> None:
        from horizon.tools.file_ops import _post_edit_diagnostics

        path = "/work/code.py"
        env = _StubEnv(_Result(exit_code=1, stdout=_ruff_json(path, 50)))
        set_active_environment(env)

        block = await _post_edit_diagnostics(path)

        assert block is not None
        # 20 findings rendered; overflow noted
        assert block.count("undefined name") == 20
        assert "more" in block.lower()

    async def test_no_findings_returns_none(self) -> None:
        from horizon.tools.file_ops import _post_edit_diagnostics

        env = _StubEnv(_Result(exit_code=0, stdout="[]"))
        set_active_environment(env)

        assert await _post_edit_diagnostics("/work/code.py") is None

    async def test_ruff_missing_returns_none(self) -> None:
        from horizon.tools.file_ops import _post_edit_diagnostics

        env = _StubEnv(
            _Result(exit_code=127, stdout="", stderr="ruff: command not found")
        )
        set_active_environment(env)

        assert await _post_edit_diagnostics("/work/code.py") is None

    async def test_execute_raises_returns_none(self) -> None:
        from horizon.tools.file_ops import _post_edit_diagnostics

        env = _StubEnv(RuntimeError("transport boom"))
        set_active_environment(env)

        assert await _post_edit_diagnostics("/work/code.py") is None

    async def test_unparseable_json_returns_none(self) -> None:
        from horizon.tools.file_ops import _post_edit_diagnostics

        env = _StubEnv(_Result(exit_code=1, stdout="not json at all"))
        set_active_environment(env)

        assert await _post_edit_diagnostics("/work/code.py") is None


class _RWStubEnv(_StubEnv):
    """Adds read/write so edit/write (the tools) can run against the stub.

    Implements the Environment interface's read_file/write_file, which the
    tools call internally — these must keep their Environment-API names,
    not the horizon tool names.
    """

    def __init__(self, result, files: dict[str, bytes] | None = None) -> None:
        super().__init__(result)
        self.files: dict[str, bytes] = files or {}

    async def read_file(self, path):
        key = str(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    async def write_file(self, path, content):
        self.files[str(path)] = (
            content.encode() if isinstance(content, str) else content
        )

    @property
    def working_dir(self):
        from pathlib import Path

        return Path("/")


class TestDiagnosticsIntegration:
    async def test_patch_attaches_diagnostics_for_py(self, tmp_path) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text("x = 1\n")
        env = _RWStubEnv(
            _Result(exit_code=1, stdout=_ruff_json(str(target), 1)),
            files={str(target): b"x = 1\n"},
        )
        set_active_environment(env)

        result = await edit(
            str(target), [{"oldText": "x = 1", "newText": "x = 2"}]
        )

        assert result["success"] is True
        assert "diagnostics" in result
        assert "F821" in result["diagnostics"]

    async def test_write_file_attaches_diagnostics_for_py(
        self, tmp_path
    ) -> None:
        from horizon.tools.file_ops import write

        target = tmp_path / "code.py"
        env = _RWStubEnv(
            _Result(exit_code=1, stdout=_ruff_json(str(target), 1))
        )
        set_active_environment(env)

        result = await write(str(target), "x = 1\n")

        assert result["success"] is True
        assert "diagnostics" in result

    async def test_no_diagnostics_key_for_non_py(self, tmp_path) -> None:
        from horizon.tools.file_ops import write

        target = tmp_path / "notes.txt"
        env = _RWStubEnv(_Result(exit_code=0, stdout="[]"))
        set_active_environment(env)

        result = await write(str(target), "hello\n")

        assert result["success"] is True
        assert "diagnostics" not in result
        # ruff should never even be invoked for a .txt file
        assert not any("ruff check" in c for c in env.commands)

    async def test_clean_py_has_no_diagnostics_key(self, tmp_path) -> None:
        from horizon.tools.file_ops import write

        target = tmp_path / "clean.py"
        env = _RWStubEnv(_Result(exit_code=0, stdout="[]"))
        set_active_environment(env)

        result = await write(str(target), "x = 1\n")

        assert result["success"] is True
        assert "diagnostics" not in result
