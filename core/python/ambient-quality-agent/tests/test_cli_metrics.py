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

"""Tests for `agents-cli aqua metrics`, and for its copy of the loader.

The CLI cannot import the agent's loader: it runs under `uv run --no-project`
in a project that has no AQuA on its path. So it carries its own, and
`test_the_cli_validator_agrees_with_the_agent_loader` is what keeps the two
level -- the same arrangement `docs_reader.py` already uses.
"""

from __future__ import annotations

from typing import Any

import pytest
from ambient_quality_agent.tools.metrics.library import load_library
from ambient_quality_cli import metrics as cli_metrics

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


# --- parity with the agent's loader ----------------------------------------


def test_the_cli_validator_agrees_with_the_agent_loader() -> None:
    """Two implementations of one rule. This is what stops them drifting.

    The loader keeps remote metrics apart from the local mapping, because the
    two are run by different machines. The CLI only decides publishable or not,
    so it compares against both.
    """
    report = cli_metrics.validate(GOOD)
    library = load_library(GOOD)
    runnable = {
        **library,
        **{m.name: m for m in library.remote},
        **{m.name: m for m in library.judged},
    }

    assert {m.name for m in report.metrics} == set(runnable)
    for metric in report.metrics:
        assert metric.expected == runnable[metric.name].expected

    local = [m for m in report.metrics if m.name in library]
    for metric in local:
        assert metric.model_modules == library[metric.name].model_modules


@pytest.mark.parametrize(
    "objects",
    [
        pytest.param(
            {"unit.py": b"def evaluate(i): return 1.0\n"}, id="no config"
        ),
        pytest.param(
            {"eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics: []\n"},
            id="selects a metric nothing defines",
        ),
        pytest.param(
            {
                "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
                b"  - name: x\n    custom_function_file: missing.py\n"
            },
            id="names a file that is not there",
        ),
        pytest.param(
            {
                "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
                b"  - name: x\n    custom_function_file: x.py\n",
                "x.py": b"def evaluate(:\n",
            },
            id="does not compile",
        ),
        pytest.param(
            {
                "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
                b"  - name: x\n    custom_function_file: x.py\n",
                "x.py": b"def score(instance):\n    return 1.0\n",
            },
            id="no evaluate",
        ),
    ],
)
def test_a_library_the_cli_refuses_scores_nothing_in_the_agent(
    objects: dict[str, bytes],
) -> None:
    """The two do not fail in the same place, and should not.

    `publish` refuses, so the operator learns at their terminal. The agent
    loads an empty library instead of raising here, because the node turns that
    into the run failure -- `library.py` cannot tell a malformed library from a
    deployment nobody has published to yet.

    What must hold either way is that neither of them quietly scores with
    something. That is what this asserts.
    """
    with pytest.raises(cli_metrics.ValidationError):
        cli_metrics.validate(objects)

    try:
        assert load_library(objects) == {}
    except Exception as exc:
        assert not isinstance(exc, AssertionError)


# --- validation -------------------------------------------------------------


def test_validate_reports_what_it_skipped_and_why() -> None:
    report = cli_metrics.validate(GOOD)

    assert dict(report.skipped) == {
        "predefined_one": (
            "custom_metrics does not define it, so it names a predefined metric"
        ),
    }
    assert "judged_one" in [m.name for m in report.metrics]


def test_validate_names_the_metrics_that_cost_a_model_call() -> None:
    assert [
        m.name for m in cli_metrics.validate(GOOD).list_costly_metrics()
    ] == ["judge"]


def test_a_library_with_nothing_runnable_is_refused() -> None:
    """Publishing it would leave the deployment unable to score anything.

    A bare predefined name is the remaining unrunnable kind: remote and judged
    metrics are both run by the eval service now.
    """
    objects = {
        "eval_config.yaml": b"metrics_to_run: [predefined_one]\ncustom_metrics: []\n"
    }
    with pytest.raises(
        cli_metrics.ValidationError, match="nothing to score with"
    ):
        cli_metrics.validate(objects)


def test_a_library_of_only_remote_metrics_can_be_published() -> None:
    """The feature is unusable otherwise: an operator cannot publish the very
    metrics the eval service exists to run."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [remote_one]\ncustom_metrics:\n"
        b"  - name: remote_one\n    remote_custom_function: |\n"
        b"      def evaluate(instance):\n          return 1.0\n"
    }

    report = cli_metrics.validate(objects)

    assert [m.name for m in report.metrics] == ["remote_one"]


def test_a_library_of_only_judged_metrics_can_be_published() -> None:
    """An operator must be able to publish a library the eval service judges."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [judged_one]\ncustom_metrics:\n"
        b'  - name: judged_one\n    prompt_template: "is this good?"\n'
        b"    expected: The reply is good.\n"
    }

    report = cli_metrics.validate(objects)

    (metric,) = report.metrics
    assert metric.name == "judged_one"
    assert metric.source_path == "<prompt_template:judged_one>"
    assert metric.expected == "The reply is good."
    assert metric.judged is True
    assert report.list_judged_metrics() == (metric,)


def test_a_blank_prompt_template_is_refused_at_publish() -> None:
    """The SDK refuses it at the sweep, so the operator hears it here first."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [j]\ncustom_metrics:\n"
        b'  - name: j\n    prompt_template: "   "\n'
    }

    with pytest.raises(cli_metrics.ValidationError, match="Prompt template"):
        cli_metrics.validate(objects)


def test_a_non_finite_threshold_on_a_judge_is_refused_at_publish() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [j]\ncustom_metrics:\n"
        b'  - name: j\n    prompt_template: "good?"\n    threshold: .nan\n'
    }

    with pytest.raises(cli_metrics.ValidationError, match="non-finite"):
        cli_metrics.validate(objects)


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (
            '    prompt_template: "turns: {agent_eval_data}"\n',
            "{agent_eval_data}",
        ),
        (
            '    prompt_template: "good? {response}"\n    return_raw_output: true\n',
            "return_raw_output",
        ),
    ],
)
def test_a_judge_shape_the_service_errors_on_every_session_is_refused_at_publish(
    body: str, said: str
) -> None:
    """The loader refuses these; the publish command says so first."""
    objects = {
        "eval_config.yaml": (
            "metrics_to_run: [j]\ncustom_metrics:\n  - name: j\n" + body
        ).encode()
    }

    with pytest.raises(cli_metrics.ValidationError) as caught:
        cli_metrics.validate(objects)
    assert said in str(caught.value)


def test_the_judge_sampling_count_is_carried_for_the_cost_line() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [j]\ncustom_metrics:\n"
        b'  - name: j\n    prompt_template: "good? {response}"\n'
        b"    judge_model_sampling_count: 3\n"
    }

    (metric,) = cli_metrics.validate(objects).metrics

    assert metric.judge_samples == 3


def test_a_judge_without_a_threshold_is_published_as_scores_only() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [j]\ncustom_metrics:\n"
        b'  - name: j\n    prompt_template: "rate 1-5 {response}"\n'
    }

    (metric,) = cli_metrics.validate(objects).metrics

    assert metric.scores_only is True


def test_a_template_asking_for_the_prompt_is_flagged() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [j]\ncustom_metrics:\n"
        b'  - name: j\n    prompt_template: "user: {prompt} reply: {response}"\n'
    }

    (metric,) = cli_metrics.validate(objects).metrics

    assert metric.reads_prompt is True


def test_a_template_asking_for_the_prompt_is_published() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [j]\ncustom_metrics:\n"
        b'  - name: j\n    prompt_template: "user: {prompt} reply: {response}"\n'
    }

    assert [m.name for m in cli_metrics.validate(objects).metrics] == ["j"]


def test_a_remote_metric_returning_a_score_mapping_is_refused_before_publish() -> (
    None
):
    """The service scores that form 0.0, so every session would read as a
    defect. Catching it here is the point of validating before publishing."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [bad]\ncustom_metrics:\n"
        b"  - name: bad\n    remote_custom_function: |\n"
        b"      def evaluate(instance):\n          return {'score': 1.0}\n"
    }

    with pytest.raises(
        cli_metrics.ValidationError, match="must return a number"
    ):
        cli_metrics.validate(objects)


def test_a_score_mapping_behind_a_ternary_is_refused_too() -> None:
    """The loader reads both arms, so a validator that reads only the whole
    expression publishes a metric the loader then refuses to load."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [bad]\ncustom_metrics:\n"
        b"  - name: bad\n    remote_custom_function: |\n"
        b"      def evaluate(instance):\n"
        b"          return {'score': 1.0} if instance else 0.0\n"
    }

    with pytest.raises(
        cli_metrics.ValidationError, match="must return a number"
    ):
        cli_metrics.validate(objects)


def test_an_async_remote_evaluate_is_refused_before_publish() -> None:
    """Nothing awaits it, so it returns a coroutine and fails every session."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [bad]\ncustom_metrics:\n"
        b"  - name: bad\n    remote_custom_function: |\n"
        b"      async def evaluate(instance):\n          return 1.0\n"
    }

    with pytest.raises(cli_metrics.ValidationError, match="never awaited"):
        cli_metrics.validate(objects)


def test_a_non_finite_remote_threshold_is_refused_before_publish() -> None:
    """`score >= nan` is false for every score, so the metric reads as a defect
    storm rather than as the typo it is."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [bad]\ncustom_metrics:\n"
        b"  - name: bad\n    threshold: .nan\n    remote_custom_function: |\n"
        b"      def evaluate(instance):\n          return 1.0\n"
    }

    with pytest.raises(
        cli_metrics.ValidationError, match="non-finite threshold"
    ):
        cli_metrics.validate(objects)


def test_the_mirrored_checks_do_not_refuse_a_local_metric() -> None:
    """The loader applies all three on the remote path only. Refusing more than
    it does blocks the publish of a metric that would have run."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [local_one]\ncustom_metrics:\n"
        b"  - name: local_one\n    threshold: .nan\n    custom_function: |\n"
        b"      def evaluate(instance):\n          return {'score': 1.0}\n"
    }

    assert [m.name for m in cli_metrics.validate(objects).metrics] == [
        "local_one"
    ]


def test_validate_does_not_execute_the_metric() -> None:
    """Running operator code on the operator's laptop to tell them it parses is
    a side effect nobody asked for."""
    objects = {
        "eval_config.yaml": b"metrics_to_run: [x]\ncustom_metrics:\n"
        b"  - name: x\n    custom_function_file: x.py\n",
        "x.py": b"raise SystemExit('module scope ran')\n\n"
        b"def evaluate(instance):\n    return 1.0\n",
    }

    assert [m.name for m in cli_metrics.validate(objects).metrics] == ["x"]


# --- reading a directory ----------------------------------------------------


def test_read_directory_takes_the_publishable_files_only(tmp_path: Any) -> None:
    (tmp_path / "eval_config.yaml").write_bytes(b"metrics_to_run: []\n")
    (tmp_path / "m.py").write_bytes(b"x = 1\n")
    (tmp_path / "notes.txt").write_bytes(b"ignore me\n")
    (tmp_path / ".hidden.py").write_bytes(b"ignore me\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "m.cpython-312.pyc").write_bytes(b"\x00")
    nested = tmp_path / "helpers"
    nested.mkdir()
    (nested / "shared.py").write_bytes(b"y = 2\n")

    assert set(cli_metrics.read_directory(tmp_path)) == {
        "eval_config.yaml",
        "m.py",
        "helpers/shared.py",
    }


# --- the mirror -------------------------------------------------------------


def test_publishing_deletes_what_the_directory_no_longer_has() -> None:
    """Without this a metric the author deleted keeps scoring forever."""
    local = {"eval_config.yaml": b"", "kept.py": b""}
    remote = [
        {"name": "current/metrics/eval_config.yaml"},
        {"name": "current/metrics/kept.py"},
        {"name": "current/metrics/removed.py"},
    ]

    uploads, deletions = cli_metrics.compute_mirror_plan(local, remote)

    # Config last: it names the modules, so a publish killed part-way must not
    # leave a config advertising files that are not uploaded yet.
    assert uploads == ["kept.py", "eval_config.yaml"]
    assert deletions == ["current/metrics/removed.py"]


def test_an_empty_bucket_deletes_nothing() -> None:
    uploads, deletions = cli_metrics.compute_mirror_plan({"a.py": b""}, [])

    assert (uploads, deletions) == (["a.py"], [])


# --- error messages the operator has to act on ------------------------------


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = "{}"

    def json(self) -> dict:
        return {}


def test_a_403_names_the_grant_that_is_missing() -> None:
    """The module grants write to nobody by default, so this is the common
    first failure and "403" alone does not tell them what to ask for."""
    with pytest.raises(cli_metrics.ValidationError, match="metrics_writers"):
        cli_metrics._raise_for_gcs(_Response(403), "b")


def test_a_404_says_to_deploy_first() -> None:
    with pytest.raises(cli_metrics.ValidationError, match="does not exist"):
        cli_metrics._raise_for_gcs(_Response(404), "b")


def test_a_success_raises_nothing() -> None:
    assert cli_metrics._raise_for_gcs(_Response(204), "b") is None
