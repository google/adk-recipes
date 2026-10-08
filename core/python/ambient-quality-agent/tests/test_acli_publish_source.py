# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Where the extension publishes the observed agent's source.

`agents-cli deploy` runs `publish-source` after a deploy, through AQuA's API,
and `agents-cli aqua attach` and `publish-source` are pointed at the project.
What `publish-source` itself does is `tests/test_cli_source_snapshot.py`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import acli_aqua
import acli_deploy
import pytest

ENGINE = "projects/p/locations/us-east1/reasoningEngines/42"


class _Recorder:
    """Test double for `_acli.run` that records executed commands."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.returncode = 0

    def __call__(self, argv, *, cwd=None, env=None, capture=False):
        self.calls.append(list(argv))
        if capture:
            return subprocess.CompletedProcess(list(argv), 0, "{}", "")
        return subprocess.CompletedProcess(list(argv), self.returncode, "", "")

    def publishes(self) -> list[list[str]]:
        return [call for call in self.calls if "publish-source" in call]


@pytest.fixture
def deploy(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """The deploy wrapper in an observed agent's project, attached to an AQuA."""
    recorder = _Recorder()
    monkeypatch.setattr(acli_deploy, "run", recorder)
    monkeypatch.setattr(acli_deploy, "run_builtin", lambda command, argv: None)
    monkeypatch.setattr(acli_deploy, "find_project_root", lambda: Path("/proj"))
    # Pinned because pytest runs inside AQuA's own checkout.
    monkeypatch.setattr(acli_deploy, "is_aqua_own_checkout", lambda: False)
    monkeypatch.setattr(
        acli_deploy, "ensure_state_dir_ignored", lambda root: None
    )
    monkeypatch.setattr(acli_aqua, "read_recorded_engine_id", lambda: ENGINE)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    return recorder


def test_deploy_publishes_through_aquas_cli(deploy: _Recorder) -> None:
    assert acli_deploy.main([]) == 0

    assert deploy.publishes() == [
        [
            *acli_deploy._AQUA_CLI_COMMAND,
            "publish-source",
            "--source-root=/proj",
        ]
    ]


def test_the_cli_runs_with_the_same_imports_as_the_aqua_command() -> None:
    """`acli_deploy` repeats the manifest's `--with` list; they must agree."""
    manifest = (
        Path(__file__).resolve().parents[1] / "agents-cli-extension.yaml"
    ).read_text(encoding="utf-8")
    command = acli_deploy._AQUA_CLI_COMMAND
    withs = [command[i + 1] for i, arg in enumerate(command) if arg == "--with"]

    for name in withs:
        assert f'"--with", "{name}"' in manifest
    assert manifest.count('"--with"') == len(withs)


@pytest.mark.parametrize("flag", acli_deploy._READ_ONLY_FLAGS)
def test_a_read_only_deploy_publishes_nothing(
    deploy: _Recorder, flag: str
) -> None:
    acli_deploy.main([flag])

    assert deploy.publishes() == []


def test_a_no_wait_deploy_publishes_nothing_and_says_how(
    deploy: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    acli_deploy.main(["--no-wait"])

    assert deploy.publishes() == []
    assert "agents-cli aqua publish-source" in capsys.readouterr().err


def test_without_an_aqua_there_is_nothing_to_publish_to(
    deploy: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(acli_aqua, "read_recorded_engine_id", lambda: None)

    assert acli_deploy.main([]) == 0

    assert deploy.publishes() == []
    assert "agents-cli aqua attach" in capsys.readouterr().err


def test_a_failed_publish_warns_and_does_not_fail_the_deploy(
    deploy: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy.returncode = 1

    assert acli_deploy.main([]) == 0
    assert "could not publish" in capsys.readouterr().err


def test_a_publisher_that_cannot_start_does_not_fail_the_deploy(
    deploy: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise OSError("uv is not installed")

    monkeypatch.setattr(acli_deploy, "run", explode)

    assert acli_deploy.main([]) == 0


# --- the project as the source ---------------------------------------------- #


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(acli_aqua, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(acli_aqua, "is_aqua_own_checkout", lambda: False)
    return tmp_path


def test_publish_source_is_pointed_at_the_project(project: Path) -> None:
    assert acli_aqua.add_source_root("publish-source", ["a"]) == [
        "a",
        f"--source-root={project}",
    ]


def test_attach_takes_a_snapshot_only_of_a_deployed_agent(
    project: Path,
) -> None:
    assert acli_aqua.add_source_root("attach", ["--yes"]) == ["--yes"]

    (project / acli_aqua.METADATA_FILE).write_text("{}", encoding="utf-8")

    assert acli_aqua.add_source_root("attach", ["--yes", "--", "x"]) == [
        "--yes",
        f"--source-root={project}",
        "--",
        "x",
    ]


@pytest.mark.parametrize(
    "argv", [["--source-root", "/elsewhere"], ["--source-root=/x"], ["--help"]]
)
def test_a_given_source_root_or_help_is_left_alone(
    project: Path, argv: list[str]
) -> None:
    assert acli_aqua.add_source_root("publish-source", argv) == argv


def test_aquas_own_checkout_has_no_source_to_publish(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acli_aqua, "is_aqua_own_checkout", lambda: True)

    assert acli_aqua.add_source_root("publish-source", []) == []


def test_an_agent_attached_elsewhere_publishes_on_a_plain_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`attach --aqua-resource` recorded the AQuA; `deploy` needs no flag.

    The record is read for real: a project with AQuA added as an extension,
    attached to an AQuA deployed elsewhere, and deployed without
    `--deploy-aqua`.
    """
    recorder = _Recorder()
    monkeypatch.setattr(acli_deploy, "run", recorder)
    monkeypatch.setattr(acli_deploy, "run_builtin", lambda command, argv: None)
    monkeypatch.setattr(acli_deploy, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(acli_deploy, "is_aqua_own_checkout", lambda: False)
    monkeypatch.setattr(
        acli_deploy, "ensure_state_dir_ignored", lambda root: None
    )
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".aqua").mkdir()
    (tmp_path / ".aqua" / acli_aqua.ATTACHED_AQUA_FILE).write_text(
        json.dumps({"aqua_resource": ENGINE}), encoding="utf-8"
    )

    assert acli_aqua.read_recorded_engine_id() == ENGINE
    assert acli_deploy.main([]) == 0

    assert recorder.publishes() == [
        [
            *acli_deploy._AQUA_CLI_COMMAND,
            "publish-source",
            f"--source-root={tmp_path}",
        ]
    ]
