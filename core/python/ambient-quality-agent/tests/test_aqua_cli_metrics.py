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

"""Hermetic tests for `agents-cli aqua metrics`: the library goes through the API.

The deployment is a fake client answering the `metrics/*` routes, so these pin
what the CLI sends, what it asks before, and what it prints. What the engine
does with a library is tested in `test_metrics_publishing.py`.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import metrics as metrics_lib
from ambient_quality_shared import protocol
from click.testing import CliRunner

_REPORT = {
    "metrics": [
        {
            "name": "ok",
            "source_path": "ok.py",
            "expected": "It is ok.",
            "model_modules": ["google.genai"],
            "judged": False,
        },
        {
            "name": "j",
            "source_path": "<prompt_template:j>",
            "expected": "Good.",
            "model_modules": [],
            "judged": True,
            "judge_samples": 3,
            "scores_only": True,
            "reads_prompt": True,
        },
    ],
    "skipped": [{"name": "builtin", "reason": "it names a predefined metric"}],
}


class _FakeClient:
    """Answers each route from `answers` and records `(path, payload)`."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers
        self.calls: list[tuple[str, Any]] = []

    async def post_command(self, path: str, payload: Any = None) -> Any:
        self.calls.append((str(path), payload))
        answer = self._answers[str(path)]
        return answer(payload) if callable(answer) else answer


def _build_plan_answer(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "location": "gs://m/current/metrics/",
        "report": _REPORT,
        "uploads": sorted(f["path"] for f in payload["files"]),
        "deletions": ["current/metrics/old.py"],
        "published": not payload.get("dry_run"),
    }


@pytest.fixture
def library_dir(tmp_path: Any) -> Any:
    (tmp_path / "eval_config.yaml").write_bytes(b"metrics_to_run: [ok]\n")
    (tmp_path / "ok.py").write_bytes(b"def evaluate(i):\n    return 1.0\n")
    return tmp_path


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    args: list[str],
    stdin: str | None = None,
) -> Any:
    monkeypatch.setattr(aqua_cli, "_build_client", lambda *a, **k: client)
    return CliRunner().invoke(
        aqua_cli.cli, ["metrics", *args, "--resource", "r"], input=stdin
    )


# --- publish ----------------------------------------------------------------


def test_publish_sends_the_directory_and_prints_the_engines_report(
    monkeypatch: pytest.MonkeyPatch, library_dir: Any
) -> None:
    client = _FakeClient({"metrics/publish": _build_plan_answer})

    res = _invoke(monkeypatch, client, ["publish", str(library_dir), "--yes"])

    assert res.exit_code == 0, res.output
    assert [payload["dry_run"] for _, payload in client.calls] == [True, False]
    sent = {
        f["path"]: base64.b64decode(f["content"])
        for f in client.calls[0][1]["files"]
    }
    assert sent == {
        "eval_config.yaml": b"metrics_to_run: [ok]\n",
        "ok.py": b"def evaluate(i):\n    return 1.0\n",
    }
    assert "2 metric(s) AQuA will run:" in res.output
    assert "builtin: it names a predefined metric" in res.output
    assert "ok: google.genai" in res.output
    assert "3 model call(s) per session per sweep: j (3 samples)" in res.output
    assert "j: scores only" in res.output
    assert "delete  current/metrics/old.py" in res.output
    assert "Published 2 object(s), deleted 1." in res.output


def test_publish_asks_before_deleting(
    monkeypatch: pytest.MonkeyPatch, library_dir: Any
) -> None:
    client = _FakeClient({"metrics/publish": _build_plan_answer})

    res = _invoke(
        monkeypatch, client, ["publish", str(library_dir)], stdin="n\n"
    )

    assert res.exit_code != 0
    assert [payload["dry_run"] for _, payload in client.calls] == [True]


def test_a_dry_run_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, library_dir: Any
) -> None:
    client = _FakeClient({"metrics/publish": _build_plan_answer})

    res = _invoke(
        monkeypatch, client, ["publish", str(library_dir), "--dry-run"]
    )

    assert res.exit_code == 0, res.output
    assert len(client.calls) == 1
    assert "Dry run: nothing was uploaded." in res.output


def test_a_library_the_engine_refuses_is_not_published(
    monkeypatch: pytest.MonkeyPatch, library_dir: Any
) -> None:
    client = _FakeClient(
        {"metrics/publish": {"error": "Refusing to publish: metric 'ok': boom"}}
    )

    res = _invoke(monkeypatch, client, ["publish", str(library_dir), "--yes"])

    assert res.exit_code == 1
    assert "Refusing to publish: metric 'ok': boom" in res.output
    assert len(client.calls) == 1


def test_a_library_over_the_body_cap_is_refused_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch, library_dir: Any
) -> None:
    (library_dir / "fixtures.json").write_bytes(b"0" * protocol.MAX_BODY_BYTES)
    client = _FakeClient({})

    res = _invoke(monkeypatch, client, ["publish", str(library_dir)])

    assert res.exit_code == 1
    assert "AQuA accepts at most 1024 KiB" in res.output
    assert client.calls == []


def test_an_empty_directory_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    res = _invoke(monkeypatch, _FakeClient({}), ["publish", str(tmp_path)])

    assert res.exit_code == 1
    assert "no .py, .yaml, .yml, .json file" in res.output


# --- list and destroy -------------------------------------------------------


def test_list_prints_the_files_and_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {
            "metrics/list": {
                "location": "gs://m/current/metrics/",
                "files": [{"path": "ok.py", "size": 30}],
                "report": _REPORT,
            }
        }
    )

    res = _invoke(monkeypatch, client, ["list"])

    assert res.exit_code == 0, res.output
    assert "gs://m/current/metrics/" in res.output
    assert "ok.py  30B" in res.output
    assert "2 metric(s) AQuA will run:" in res.output


def test_list_shows_a_broken_librarys_files_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {
            "metrics/list": {
                "location": "gs://m/current/metrics/",
                "files": [{"path": "x.py", "size": 6}],
                "error": "The published library is broken: no eval config",
            }
        }
    )

    res = _invoke(monkeypatch, client, ["list"])

    assert res.exit_code == 1
    assert "x.py  6B" in res.output
    assert "The published library is broken" in res.output


def test_list_of_nothing_published(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        {"metrics/list": {"location": "l", "files": [], "report": {}}}
    )

    res = _invoke(monkeypatch, client, ["list"])

    assert res.exit_code == 0
    assert "nothing to score with" in res.output


def test_destroy_plans_unless_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    def build_answer(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "location": "l",
            "deletions": ["current/metrics/ok.py"],
            "deleted": not payload["dry_run"],
        }

    client = _FakeClient({"metrics/destroy": build_answer})

    planned = _invoke(monkeypatch, client, ["destroy"])
    applied = _invoke(monkeypatch, client, ["destroy", "--apply"])

    assert "would delete  current/metrics/ok.py" in planned.output
    assert "Deleted 1 object(s)." in applied.output
    assert [payload for _, payload in client.calls] == [
        {"dry_run": True},
        {"dry_run": False},
    ]


def test_an_engine_without_a_metrics_bucket_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {"metrics/destroy": {"error": "This deployment has no metrics bucket"}}
    )

    res = _invoke(monkeypatch, client, ["destroy"])

    assert res.exit_code == 1
    assert "no metrics bucket" in res.output


# --- reading a directory ----------------------------------------------------


def test_read_directory_takes_the_publishable_files_only(tmp_path: Any) -> None:
    (tmp_path / "eval_config.yaml").write_bytes(b"metrics_to_run: []\n")
    (tmp_path / "m.py").write_bytes(b"x = 1\n")
    (tmp_path / "notes.txt").write_bytes(b"ignore me\n")
    (tmp_path / ".hidden.py").write_bytes(b"ignore me\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "m.cpython-312.pyc").write_bytes(b"\x00")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "site.py").write_bytes(b"ignore me\n")
    nested = tmp_path / "helpers"
    nested.mkdir()
    (nested / "shared.py").write_bytes(b"y = 2\n")

    assert set(metrics_lib.read_directory(tmp_path)) == {
        "eval_config.yaml",
        "m.py",
        "helpers/shared.py",
    }


def test_read_directory_reads_a_library_kept_under_a_dot_directory(
    tmp_path: Any,
) -> None:
    root = tmp_path / ".config" / "metrics"
    root.mkdir(parents=True)
    (root / "m.py").write_bytes(b"x = 1\n")

    assert set(metrics_lib.read_directory(root)) == {"m.py"}


def test_the_cli_sends_only_what_the_engine_accepts() -> None:
    from ambient_quality_agent.tools.metrics import publishing

    assert metrics_lib.PUBLISHABLE_SUFFIXES == publishing.PUBLISHABLE_SUFFIXES


def test_list_says_when_the_default_agent_reads_the_instances_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {
            "metrics/list": {
                "location": "gs://m/current/metrics/",
                "inherited": True,
                "files": [{"path": "ok.py", "size": 30}],
                "report": _REPORT,
            }
        }
    )

    res = _invoke(monkeypatch, client, ["list"])

    assert res.exit_code == 0, res.output
    assert "the instance's library, which the default agent" in res.output


def _build_inheriting_plan_answer(payload: dict[str, Any]) -> dict[str, Any]:
    """Builds the default agent's publish answer, replacing the instance library.

    Args:
        payload: The publish request.

    Returns:
        The route's answer.
    """
    return {
        **_build_plan_answer(payload),
        "deletions": [],
        "inherited_deletions": ["current/metrics/ok.py"],
    }


@pytest.mark.parametrize(
    ("stdin", "published"),
    [("y\n", True), ("n\n", False)],
)
def test_publish_asks_before_deleting_the_instances_library(
    monkeypatch: pytest.MonkeyPatch,
    library_dir: Any,
    stdin: str,
    published: bool,
) -> None:
    client = _FakeClient({"metrics/publish": _build_inheriting_plan_answer})

    res = _invoke(
        monkeypatch, client, ["publish", str(library_dir)], stdin=stdin
    )

    assert "The instance's library, which this agent scores with" in res.output
    assert "Delete the instance's library (1 object(s))" in res.output
    assert (res.exit_code == 0) is published, res.output
    assert [p["dry_run"] for _, p in client.calls] == (
        [True, False] if published else [True]
    )
    if published:
        assert "Published 2 object(s), deleted 1." in res.output


# --- the agent --------------------------------------------------------------


_ANSWERS = {
    "metrics/publish": _build_plan_answer,
    "metrics/list": {"location": "l", "files": [], "report": {}},
    "metrics/destroy": {"location": "l", "deletions": []},
}


@pytest.mark.parametrize(
    "command", [["publish", ".", "--yes"], ["list"], ["destroy", "--apply"]]
)
def test_every_metrics_command_sends_the_agent_key(
    monkeypatch: pytest.MonkeyPatch,
    library_dir: Any,
    command: list[str],
) -> None:
    monkeypatch.chdir(library_dir)
    client = _FakeClient(_ANSWERS)

    res = _invoke(monkeypatch, client, [*command, "--agent-key", "p__bot"])

    assert res.exit_code == 0, res.output
    assert client.calls
    assert all(payload["agent_key"] == "p__bot" for _, payload in client.calls)


def test_metrics_publish_from_the_agents_project_takes_the_recorded_key(
    monkeypatch: pytest.MonkeyPatch, library_dir: Any
) -> None:
    """The library lands under the agent `attach` recorded here, with no flag."""
    state = library_dir / ".aqua"
    state.mkdir()
    (state / "attached_aqua.json").write_text(
        '{"agent_key": "p__bot", "agent_key_target": "r"}', encoding="utf-8"
    )
    monkeypatch.chdir(library_dir)
    client = _FakeClient(_ANSWERS)

    res = _invoke(monkeypatch, client, ["publish", ".", "--yes"])

    assert res.exit_code == 0, res.output
    assert [payload["agent_key"] for _, payload in client.calls] == [
        "p__bot",
        "p__bot",
    ]
