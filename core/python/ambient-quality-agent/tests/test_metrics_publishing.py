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

"""Tests for `metrics.publishing`: what `aqua metrics` does behind the routes.

The library is validated by the loader that runs it, so what a publish refuses
is what a run would fail on. The store is the autouse `metrics_store`
directory, so the object names asserted here are the ones a bucket gets.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from ambient_quality_agent.tools.metrics import publishing
from ambient_quality_agent.tools.objects import store as objects_store

GOOD = {
    "eval_config.yaml": b"""
metrics_to_run: [unit, judge, predefined_one, remote_one, judged_one]
custom_metrics:
  - name: unit
    custom_function_file: unit.py
  - name: judge
    custom_function_file: judge.py
  - name: remote_one
    remote_custom_function: |
      def evaluate(instance):
          return 1.0
  - name: judged_one
    prompt_template: "is this good?"
""",
    "unit.py": b'EXPECTED = "Every temperature carries a unit."\n'
    b"def evaluate(instance):\n    return 1.0\n",
    "judge.py": b"from google import genai\n\ndef evaluate(instance):\n    return 1.0\n",
}

_PREFIX = publishing.LIBRARY_PREFIX


def _encode_files(objects: dict[str, bytes]) -> list[dict[str, str]]:
    return [
        {"path": path, "content": base64.b64encode(data).decode()}
        for path, data in objects.items()
    ]


def _publish_library(
    objects: dict[str, bytes], *, dry_run: bool = True
) -> dict:
    return publishing.publish(
        _encode_files(objects), agent_key="", dry_run=dry_run
    )


def _list_report_metrics(
    objects: dict[str, bytes],
) -> dict[str, dict[str, Any]]:
    """Publishes as a dry run and returns the report's metrics.

    Args:
        objects: Each file's content, by its path in the library.

    Returns:
        The report's metrics, by name.
    """
    result = _publish_library(objects)
    assert "error" not in result, result
    return {m["name"]: m for m in result["report"]["metrics"]}


def _build_judge_library(body: str) -> dict[str, bytes]:
    return {
        "eval_config.yaml": (
            "metrics_to_run: [j]\ncustom_metrics:\n  - name: j\n" + body
        ).encode()
    }


def _build_remote_library(body: str, extra: str = "") -> dict[str, bytes]:
    return {
        "eval_config.yaml": (
            "metrics_to_run: [bad]\ncustom_metrics:\n  - name: bad\n"
            + extra
            + "    remote_custom_function: |\n"
            + body
        ).encode()
    }


# --- the report -------------------------------------------------------------


def test_the_report_names_what_runs_and_what_is_skipped() -> None:
    result = _publish_library(GOOD)

    report = result["report"]
    by_name = {m["name"]: m for m in report["metrics"]}
    assert set(by_name) == {"unit", "judge", "remote_one", "judged_one"}
    assert by_name["unit"]["source_path"] == "unit.py"
    assert by_name["unit"]["expected"] == "Every temperature carries a unit."
    assert by_name["judge"]["model_modules"] == ["google.genai"]
    assert report["skipped"] == [
        {
            "name": "predefined_one",
            "reason": "custom_metrics does not define it, so it names a "
            "predefined metric",
        }
    ]


def test_a_library_of_only_remote_metrics_can_be_published() -> None:
    """The feature is unusable otherwise: an operator cannot publish the very
    metrics the eval service exists to run."""
    metrics = _list_report_metrics(
        _build_remote_library(
            "      def evaluate(instance):\n          return 1.0\n"
        )
    )

    assert list(metrics) == ["bad"]
    assert metrics["bad"]["source_path"] == "<custom_function:bad>"


def test_a_judged_metric_reports_what_it_costs_and_how_it_scores() -> None:
    metrics = _list_report_metrics(
        _build_judge_library(
            '    prompt_template: "user: {prompt} reply: {response}"\n'
            "    expected: The reply is good.\n"
            "    judge_model_sampling_count: 3\n"
        )
    )

    assert metrics["j"] == {
        "name": "j",
        "source_path": "<prompt_template:j>",
        "expected": "The reply is good.",
        "model_modules": [],
        "judged": True,
        "judge_samples": 3,
        "scores_only": True,
        "reads_prompt": True,
    }


def test_a_judge_with_a_threshold_files_findings() -> None:
    metrics = _list_report_metrics(
        _build_judge_library(
            '    prompt_template: "good? {response}"\n    threshold: 0.5\n'
        )
    )

    assert metrics["j"]["scores_only"] is False


def test_a_local_metric_with_a_mapping_and_a_nan_threshold_is_published() -> (
    None
):
    """The loader applies the remote checks on the remote path only, and a
    publish refuses no more than a run would fail on."""
    metrics = _list_report_metrics(
        {
            "eval_config.yaml": b"metrics_to_run: [local_one]\ncustom_metrics:\n"
            b"  - name: local_one\n    threshold: .nan\n    custom_function: |\n"
            b"      def evaluate(instance):\n          return {'score': 1.0}\n"
        }
    )

    assert list(metrics) == ["local_one"]


# --- what a publish refuses -------------------------------------------------


@pytest.mark.parametrize(
    ("objects", "said"),
    [
        pytest.param(
            {"unit.py": b"def evaluate(i): return 1.0\n"},
            "no eval config",
            id="no config",
        ),
        pytest.param(
            {"eval_config.yaml": b"- a list\n"},
            "must parse to a mapping",
            id="config is not a mapping",
        ),
        pytest.param(
            {"eval_config.yaml": b"metrics_to_run: [x\n"},
            "",
            id="config is not YAML",
        ),
        pytest.param(
            {"eval_config.yaml": b"metrics_to_run: 5\n"},
            "TypeError",
            id="metrics_to_run is not a list",
        ),
        pytest.param(
            {"eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics: 5\n"},
            "TypeError",
            id="custom_metrics is not a list",
        ),
        pytest.param(
            {"eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics: []\n"},
            "nothing to score with",
            id="selects only a predefined metric",
        ),
        pytest.param(
            {
                "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
                b"  - name: x\n    custom_function_file: missing.py\n"
            },
            "not published",
            id="names a file that is not there",
        ),
        pytest.param(
            {
                "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
                b"  - name: x\n    custom_function_file: x.py\n",
                "x.py": b"def evaluate(:\n",
            },
            "SyntaxError",
            id="does not compile",
        ),
        pytest.param(
            {
                "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
                b"  - name: x\n    custom_function_file: x.py\n",
                "x.py": b"def score(instance):\n    return 1.0\n",
            },
            "no evaluate",
            id="no evaluate",
        ),
        pytest.param(
            _build_judge_library('    prompt_template: "   "\n'),
            "Prompt template",
            id="blank prompt template",
        ),
        pytest.param(
            _build_judge_library(
                '    prompt_template: "good?"\n    threshold: .nan\n'
            ),
            "non-finite",
            id="judge with a non-finite threshold",
        ),
        pytest.param(
            _build_judge_library(
                '    prompt_template: "turns: {agent_eval_data}"\n'
            ),
            "{agent_eval_data}",
            id="judge naming agent_eval_data",
        ),
        pytest.param(
            _build_judge_library(
                '    prompt_template: "good? {response}"\n'
                "    return_raw_output: true\n"
            ),
            "return_raw_output",
            id="judge returning raw output",
        ),
        pytest.param(
            _build_remote_library(
                "      def evaluate(instance):\n          return {'score': 1.0}\n"
            ),
            "must return a number",
            id="remote returning a score mapping",
        ),
        pytest.param(
            _build_remote_library(
                "      def evaluate(instance):\n"
                "          return {'score': 1.0} if instance else 0.0\n"
            ),
            "must return a number",
            id="remote returning a mapping behind a ternary",
        ),
        pytest.param(
            _build_remote_library(
                "      async def evaluate(instance):\n          return 1.0\n"
            ),
            "never awaited",
            id="remote with an async evaluate",
        ),
        pytest.param(
            _build_remote_library(
                "      def evaluate(instance):\n          return 1.0\n",
                extra="    threshold: .nan\n",
            ),
            "non-finite",
            id="remote with a non-finite threshold",
        ),
    ],
)
def test_a_library_a_run_would_fail_on_is_refused(
    objects: dict[str, bytes], said: str, metrics_store: Any
) -> None:
    result = _publish_library(objects, dry_run=False)

    assert result["error"].startswith("Refusing to publish: ")
    assert said in result["error"]
    assert metrics_store.list_names("") == []


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        ({"path": "../escape.py", "content": ""}, "not a path inside"),
        ({"path": "/abs.py", "content": ""}, "not a path inside"),
        ({"path": "a\\b.py", "content": ""}, "not a path inside"),
        ({"path": "x/../../b.py", "content": ""}, "not a path inside"),
        ({"path": "a\x00.py", "content": ""}, "control characters"),
        ({"path": "a\n.py", "content": ""}, "control characters"),
        ({"path": "notes.txt", "content": ""}, "holds only"),
        ({"path": "a.py", "content": "not base64!"}, "not base64"),
        ({"path": "a.py", "content": 3}, "not a string"),
        ("a.py", "not a JSON object"),
    ],
)
def test_a_malformed_file_is_refused(entry: Any, said: str) -> None:
    result = publishing.publish([entry], agent_key="", dry_run=True)

    assert said in result["error"]


@pytest.mark.parametrize(
    ("sent", "stored"),
    [("a/../b.py", "b.py"), ("./a.py", "a.py"), ("a//b.py", "a/b.py")],
)
def test_a_path_is_stored_normalized(
    sent: str, stored: str, metrics_store: Any
) -> None:
    files = [*_encode_files(GOOD), {"path": sent, "content": ""}]

    result = publishing.publish(files, agent_key="", dry_run=False)

    assert stored in result["uploads"]
    assert metrics_store.read_bytes(f"{_PREFIX}{stored}") == b""


def test_a_path_sent_twice_after_normalizing_is_refused() -> None:
    files = [*_encode_files({"a.py": b""}), {"path": "./a.py", "content": ""}]

    assert (
        "sent twice"
        in publishing.publish(files, agent_key="", dry_run=True)["error"]
    )


def test_a_path_sent_twice_is_refused() -> None:
    files = _encode_files({"a.py": b""}) * 2

    assert (
        "sent twice"
        in publishing.publish(files, agent_key="", dry_run=True)["error"]
    )


def test_files_that_are_not_a_list_are_refused() -> None:
    assert (
        "not a JSON list"
        in publishing.publish("x", agent_key="", dry_run=True)["error"]
    )


def test_without_a_metrics_store_nothing_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(objects_store, "metrics_store_factory", lambda: None)

    assert "no metrics bucket" in _publish_library(GOOD)["error"]
    assert (
        "no metrics bucket" in publishing.list_published(agent_key="")["error"]
    )
    assert (
        "no metrics bucket"
        in publishing.destroy(agent_key="", dry_run=True)["error"]
    )


# --- the mirror -------------------------------------------------------------


def test_a_dry_run_plans_and_writes_nothing(metrics_store: Any) -> None:
    metrics_store.write_bytes(f"{_PREFIX}removed.py", b"x = 1\n")

    result = _publish_library(GOOD)

    assert result["published"] is False
    assert result["uploads"][-1] == "eval_config.yaml"
    assert result["deletions"] == [f"{_PREFIX}removed.py"]
    assert metrics_store.list_names("") == [f"{_PREFIX}removed.py"]


def test_publishing_mirrors_the_directory(metrics_store: Any) -> None:
    """Without the deletion a metric the author deleted keeps scoring forever."""
    metrics_store.write_bytes(f"{_PREFIX}removed.py", b"x = 1\n")

    result = _publish_library(GOOD, dry_run=False)

    assert result["published"] is True
    assert result["location"] == metrics_store.uri(_PREFIX)
    assert metrics_store.list_names("") == sorted(
        f"{_PREFIX}{path}" for path in GOOD
    )
    assert metrics_store.read_bytes(f"{_PREFIX}unit.py") == GOOD["unit.py"]


def test_the_config_is_written_last(
    metrics_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It names the modules, so a publish killed part-way must not leave a
    config naming files that are not written yet."""
    written: list[str] = []
    write = metrics_store.write_bytes

    def record(name: str, data: bytes) -> None:
        written.append(name)
        write(name, data)

    monkeypatch.setattr(metrics_store, "write_bytes", record)

    _publish_library(GOOD, dry_run=False)

    assert written[-1] == f"{_PREFIX}eval_config.yaml"


def test_a_published_library_is_what_a_run_loads(metrics_store: Any) -> None:
    _publish_library(GOOD, dry_run=False)

    library = publishing.load_published_library(metrics_store, "")

    assert set(library) == {"unit", "judge"}
    assert [m.name for m in library.remote] == ["remote_one"]
    assert [m.name for m in library.judged] == ["judged_one"]


def test_nothing_published_loads_an_empty_library(metrics_store: Any) -> None:
    assert publishing.load_published_library(metrics_store, "") == {}


# --- list and destroy -------------------------------------------------------


def test_list_reports_the_published_library(metrics_store: Any) -> None:
    _publish_library(GOOD, dry_run=False)

    listed = publishing.list_published(agent_key="")

    assert [f["path"] for f in listed["files"]] == sorted(GOOD)
    assert {m["name"] for m in listed["report"]["metrics"]} == {
        "unit",
        "judge",
        "remote_one",
        "judged_one",
    }


def test_list_of_nothing_published_is_empty() -> None:
    listed = publishing.list_published(agent_key="")

    assert listed["files"] == []
    assert listed["report"] == {}
    assert "error" not in listed


def test_list_names_a_broken_library_with_its_files(metrics_store: Any) -> None:
    metrics_store.write_bytes(f"{_PREFIX}x.py", b"x = 1\n")

    listed = publishing.list_published(agent_key="")

    assert [f["path"] for f in listed["files"]] == ["x.py"]
    assert listed["error"].startswith("The published library is broken:")


def test_destroy_plans_then_deletes(metrics_store: Any) -> None:
    _publish_library(GOOD, dry_run=False)
    names = sorted(f"{_PREFIX}{path}" for path in GOOD)

    planned = publishing.destroy(agent_key="", dry_run=True)

    assert planned["deletions"] == names
    assert metrics_store.list_names("") == names

    destroyed = publishing.destroy(agent_key="", dry_run=False)

    assert destroyed["deleted"] is True
    assert metrics_store.list_names("") == []


def test_a_metric_in_a_subdirectory_keeps_its_relative_name(
    metrics_store: Any,
) -> None:
    """`custom_function_file` is resolved against the library root, so a metric
    in a subdirectory has to be stored under the path the config names."""
    library = {
        "eval_config.yaml": b"metrics_to_run: [ok]\ncustom_metrics:\n"
        b"  - name: ok\n    custom_function_file: helpers/ok.py\n",
        "helpers/ok.py": b"def evaluate(instance):\n    return 1.0\n",
    }

    _publish_library(library, dry_run=False)

    assert set(publishing.load_published_library(metrics_store, "")) == {"ok"}


class _PlaceholderStore:
    """A bucket listing that includes a directory placeholder, as the console
    creates; `FileObjectStore` cannot hold one."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = {_PREFIX: b"", f"{_PREFIX}helpers/": b"", **objects}

    def list_names(self, prefix: str) -> list[str]:
        return sorted(n for n in self._objects if n.startswith(prefix))

    def read_bytes(self, name: str) -> bytes | None:
        return self._objects.get(name)

    def uri(self, name: str) -> str:
        return f"gs://m/{name}"


def test_a_directory_placeholder_is_not_a_library_file() -> None:
    store = _PlaceholderStore(
        {f"{_PREFIX}{path}": d for path, d in GOOD.items()}
    )

    library = publishing.load_published_library(store, "")  # type: ignore[arg-type]

    assert set(library) == {"unit", "judge"}


# --- each agent's library ---------------------------------------------------

_OTHER = {
    "eval_config.yaml": b"metrics_to_run: [other]\ncustom_metrics:\n"
    b"  - name: other\n    custom_function_file: other.py\n",
    "other.py": b"def evaluate(instance):\n    return 1.0\n",
}


@pytest.fixture
def default_agent(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make `support`, an agent in AQuA's own project, the default agent."""
    from ambient_quality_agent.tools.documents import agent_scope

    monkeypatch.setattr(
        agent_scope, "resolve_default_agent_key", lambda *_: "support"
    )
    return "support"


def _publish_for(agent_key: str, objects: dict[str, bytes]) -> dict[str, Any]:
    """Publishes a library for an agent.

    Args:
        agent_key: Key of the agent.
        objects: Each file's content, by its path in the library.

    Returns:
        The publish result.
    """
    return publishing.publish(
        _encode_files(objects), agent_key=agent_key, dry_run=False
    )


def test_an_agents_library_is_its_own(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_for("acme-prod__support", GOOD)

    assert metrics_store.list_names("acme-prod__support/current/metrics/")
    assert (
        publishing.load_published_library(
            metrics_store, "acme-staging__support"
        )
        == {}
    )
    assert set(
        publishing.load_published_library(metrics_store, "acme-prod__support")
    ) == {"unit", "judge"}


def test_the_default_agent_reads_the_instances_library_while_its_own_is_empty(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_library(GOOD, dry_run=False)

    listed = publishing.list_published(agent_key=default_agent)

    assert set(
        publishing.load_published_library(metrics_store, default_agent)
    ) == {
        "unit",
        "judge",
    }
    assert (
        publishing.load_published_library(metrics_store, "acme-prod__support")
        == {}
    )
    assert listed["inherited"] is True
    assert listed["location"] == metrics_store.uri(_PREFIX)


def test_the_default_agents_own_library_is_never_mixed_with_the_instances(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_library(GOOD, dry_run=False)
    metrics_store.write_bytes(
        "support/current/metrics/eval_config.yaml", _OTHER["eval_config.yaml"]
    )
    metrics_store.write_bytes(
        "support/current/metrics/other.py", _OTHER["other.py"]
    )

    library = publishing.load_published_library(metrics_store, default_agent)

    assert set(library) == {"other"}


def test_publishing_for_the_default_agent_deletes_the_instances_library(
    metrics_store: Any, default_agent: str
) -> None:
    """Otherwise it would come back once the agent's own were emptied."""
    _publish_library(GOOD, dry_run=False)

    result = _publish_for(default_agent, _OTHER)

    assert result["deletions"] == []
    assert f"{_PREFIX}unit.py" in result["inherited_deletions"]
    assert metrics_store.list_names(_PREFIX) == []
    assert metrics_store.list_names("support/current/metrics/") == [
        "support/current/metrics/eval_config.yaml",
        "support/current/metrics/other.py",
    ]


def test_publishing_for_another_agent_leaves_the_instances_library(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_library(GOOD, dry_run=False)

    result = _publish_for("acme-prod__support", _OTHER)

    assert result["deletions"] == []
    assert result["inherited_deletions"] == []
    assert metrics_store.list_names(_PREFIX)


def test_destroying_the_default_agents_library_deletes_both(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_library(GOOD, dry_run=False)
    _publish_for("acme-prod__support", _OTHER)
    metrics_store.write_bytes("support/current/metrics/other.py", b"")

    publishing.destroy(agent_key=default_agent, dry_run=False)

    assert metrics_store.list_names("") == [
        "acme-prod__support/current/metrics/eval_config.yaml",
        "acme-prod__support/current/metrics/other.py",
    ]


def test_a_key_stays_one_path_segment() -> None:
    assert publishing.build_library_prefix("a/b") == "a_b/current/metrics/"
    assert publishing.build_library_prefix("") == _PREFIX


def test_a_dry_run_for_the_default_agent_plans_the_instances_deletion(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_library(GOOD, dry_run=False)

    result = publishing.publish(
        _encode_files(_OTHER), agent_key=default_agent, dry_run=True
    )

    assert sorted(result["inherited_deletions"]) == sorted(
        f"{_PREFIX}{path}" for path in GOOD
    )
    assert metrics_store.list_names(_PREFIX)


def test_an_agent_reading_its_own_library_is_not_inherited(
    metrics_store: Any, default_agent: str
) -> None:
    _publish_library(GOOD, dry_run=False)
    _publish_for("acme-prod__support", _OTHER)

    assert (
        publishing.list_published(agent_key="acme-prod__support")["inherited"]
        is False
    )


def test_an_empty_instance_library_is_not_reported_as_inherited(
    metrics_store: Any, default_agent: str
) -> None:
    listed = publishing.list_published(agent_key=default_agent)

    assert listed["inherited"] is False
    assert listed["location"] == metrics_store.uri("support/current/metrics/")


def test_unreadable_attachments_are_an_error_not_another_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.tools.documents import agent_scope

    def fail() -> str:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(agent_scope, "resolve_default_agent_key", fail)

    for result in (
        publishing.publish(
            _encode_files(GOOD), agent_key="support", dry_run=True
        ),
        publishing.list_published(agent_key="support"),
        publishing.destroy(agent_key="support", dry_run=True),
    ):
        assert "bucket unreachable" in result["error"]


def test_list_omits_directory_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PlaceholderStore(
        {f"{_PREFIX}{path}": d for path, d in GOOD.items()}
    )
    monkeypatch.setattr(objects_store, "metrics_store_factory", lambda: store)

    listed = publishing.list_published(agent_key="")

    assert [f["path"] for f in listed["files"]] == sorted(GOOD)
