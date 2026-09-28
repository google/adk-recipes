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

"""``build_environment_hints`` now appends a runtime/OS sentence."""

from __future__ import annotations

from pathlib import Path

from app.conversation import system_prompt
from app.conversation.system_prompt import (
    _build_runtime_env_sentence,
    build_environment_hints,
)


def test_runtime_sentence_mentions_system_machine_and_shell(monkeypatch):
    monkeypatch.setattr(system_prompt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_prompt.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(system_prompt.platform, "release", lambda: "6.5.0")
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")

    sentence = _build_runtime_env_sentence()
    assert "Linux" in sentence
    assert "6.5.0" in sentence
    assert "x86_64" in sentence
    assert "/usr/bin/zsh" in sentence


def test_darwin_uses_mac_ver_not_kernel_release(monkeypatch):
    monkeypatch.setattr(system_prompt.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(system_prompt.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        system_prompt.platform,
        "mac_ver",
        lambda: ("14.5", ("", "", ""), "arm64"),
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")

    sentence = _build_runtime_env_sentence()
    assert "macOS 14.5" in sentence
    assert "arm64" in sentence


def test_missing_shell_falls_back_to_unknown(monkeypatch):
    monkeypatch.setattr(system_prompt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_prompt.platform, "machine", lambda: "x86_64")
    monkeypatch.delenv("SHELL", raising=False)

    sentence = _build_runtime_env_sentence()
    assert "unknown" in sentence


def test_darwin_handles_mac_ver_exception(monkeypatch):
    monkeypatch.setattr(system_prompt.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(system_prompt.platform, "machine", lambda: "arm64")

    def boom():
        raise RuntimeError("mac_ver blew up")

    monkeypatch.setattr(system_prompt.platform, "mac_ver", boom)

    sentence = _build_runtime_env_sentence()
    assert "macOS" in sentence  # falls back to bare label


def test_hint_appends_runtime_sentence_to_cwd_line(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(system_prompt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_prompt.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(system_prompt.platform, "release", lambda: "6.5.0")
    monkeypatch.setenv("SHELL", "/bin/bash")

    hint = build_environment_hints(tmp_path)
    assert hint is not None
    assert f"Working directory: `{tmp_path}`" in hint
    assert "Runtime:" in hint
    assert "Linux 6.5.0" in hint
    assert "/bin/bash" in hint


def test_hint_still_includes_project_marker(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(system_prompt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system_prompt.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(system_prompt.platform, "release", lambda: "6.5.0")
    monkeypatch.setenv("SHELL", "/bin/bash")

    hint = build_environment_hints(tmp_path)
    assert hint is not None
    assert "Python project (pyproject.toml present)" in hint
    assert "Runtime:" in hint
