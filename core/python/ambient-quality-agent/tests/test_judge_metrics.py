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

"""1:1 agents-cli local custom metric execution support."""

from __future__ import annotations

import logging

import pytest
from ambient_quality_agent.tools.metrics import library as library_module
from ambient_quality_agent.tools.metrics import runner
from ambient_quality_agent.tools.metrics.library import load_library

from .conftest import make_agent_case, make_page


def _config(body: str) -> dict[str, bytes]:
    return {"eval_config.yaml": body.encode()}


def test_custom_metric_honours_per_metric_threshold() -> None:
    """An agents-cli local custom metric returning a 1-5 score passes when its
    score meets or exceeds its configured `threshold`."""
    library = load_library(
        _config(
            "metrics_to_run: [quality]\n"
            "custom_metrics:\n"
            "  - name: quality\n"
            "    threshold: 3\n"
            "    custom_function: |\n"
            "      def evaluate(instance):\n"
            "          return {'score': 4, 'explanation': 'good response'}\n"
        )
    )
    assert library["quality"].threshold == pytest.approx(3.0)

    tally = runner.Tally()
    finding_set = runner.score_page(
        make_page([make_agent_case("s1")], next_page_token=None), library, tally
    )
    assert finding_set.findings == []
    assert tally.passed == 1


def test_custom_metric_below_threshold_records_finding() -> None:
    library = load_library(
        _config(
            "metrics_to_run: [quality]\n"
            "custom_metrics:\n"
            "  - name: quality\n"
            "    threshold: 3\n"
            "    custom_function: |\n"
            "      EXPECTED = 'Response meets quality bar.'\n"
            "      def evaluate(instance):\n"
            "          return {'score': 2, 'explanation': 'missed key detail'}\n"
        )
    )

    tally = runner.Tally()
    finding_set = runner.score_page(
        make_page([make_agent_case("s1")], next_page_token=None), library, tally
    )
    assert len(finding_set.findings) == 1
    assert (
        finding_set.findings[0].expected_behavior
        == "Response meets quality bar."
    )
    assert finding_set.findings[0].actual_behavior == "missed key detail"
    assert tally.failed == 1


def test_a_metric_asking_for_remote_execution_is_skipped() -> None:
    """`agents-cli` sends `execution: remote` to the eval service. An
    author who wrote it meant "not in the caller's process", so running it
    in-process on the engine's credentials would break that promise."""
    library = load_library(
        _config(
            "metrics_to_run: [rem]\n"
            "custom_metrics:\n"
            "  - name: rem\n"
            "    execution: remote\n"
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
        )
    )

    assert not library


@pytest.mark.parametrize(
    "execution",
    [
        "Remote",
        "REMOTE",
        "elsewhere",
        "Local",
        # Present but null in YAML. Tests that explicit null values are rejected
        # rather than falling back to in-process execution.
        "null",
        "~",
        "",
    ],
)
def test_an_unrecognized_execution_is_refused_rather_than_run_in_process(
    execution: str,
) -> None:
    """`agents-cli` accepts `local` and `remote` and errors on anything else.

    Read as the `local` default instead, an unrecognized value runs the
    function here, on the engine's credentials. `Remote` is a plausible typo,
    and it would run precisely the code whose author asked to run it elsewhere
    in this very process, with nothing in the run saying so.
    """
    library = load_library(
        _config(
            "metrics_to_run: [m]\n"
            "custom_metrics:\n"
            "  - name: m\n"
            f"    execution: {execution}\n"
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
        )
    )

    assert not library, (
        "an unknown execution must never fall back to in-process"
    )
    (name, why) = library.failures[0]
    assert name == "m"
    assert "execution" in why


def test_a_refused_execution_also_covers_a_published_function_file() -> None:
    """The second route into the local loader, and the one operators mostly use."""
    library = load_library(
        {
            "eval_config.yaml": (
                b"metrics_to_run: [m]\n"
                b"custom_metrics:\n"
                b"  - name: m\n"
                b"    execution: Remote\n"
                b"    custom_function_file: m.py\n"
            ),
            "m.py": b"def evaluate(instance): return {'score': 1.0}\n",
        }
    )

    assert not library
    assert [name for name, _why in library.failures] == ["m"]


def test_the_execution_values_agents_cli_accepts_still_load() -> None:
    """The refusal must not catch a value a config is allowed to carry."""
    library = load_library(
        _config(
            "metrics_to_run: [m]\n"
            "custom_metrics:\n"
            "  - name: m\n"
            "    execution: local\n"
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
        )
    )

    assert list(library) == ["m"]
    assert not library.failures


def test_rubric_group_name_is_rejected_as_agents_cli_rejects_it() -> None:
    """Grading a case's `rubric_groups` needs a managed rubric metric, so this
    config fails under `agents-cli eval run`. Running it here anyway would give
    the operator a result their own tooling refuses to produce."""
    library = load_library(
        _config(
            "metrics_to_run: [a]\n"
            "custom_metrics:\n"
            "  - name: a\n"
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
            "    rubric_group_name: g\n"
        )
    )

    assert not library
    (name, why) = library.failures[0]
    assert name == "a"
    assert "rubric_group_name" in why


def test_a_name_the_service_reserves_is_rejected() -> None:
    """The service would ignore the definition and run its own metric, so the
    operator would believe a custom metric ran that never did."""
    library = load_library(
        _config(
            "metrics_to_run: [safety_v1]\n"
            "custom_metrics:\n"
            "  - name: safety_v1\n"
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
        )
    )

    assert not library
    assert [name for name, _ in library.failures] == ["safety_v1"]


def test_an_unversioned_builtin_name_overrides_it_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`agents-cli` allows this: `safety` is not registered but a versioned
    `safety_vN` is, so the published definition wins. It still warns, because
    the collision is usually an accident.

    Which N is determined by the installed SDK, so the assertion dynamically
    verifies the resolved version rather than assuming a static name.
    """
    with caplog.at_level(logging.WARNING):
        library = load_library(
            _config(
                "metrics_to_run: [safety]\n"
                "custom_metrics:\n"
                "  - name: safety\n"
                "    custom_function: |\n"
                "      def evaluate(instance): return {'score': 1.0}\n"
            )
        )

    resolved = library_module._resolve_predefined("safety")

    assert list(library) == ["safety"]
    assert resolved is not None and resolved.startswith("safety_v")
    assert resolved in caplog.text


@pytest.mark.parametrize(
    ("body", "objects"),
    [
        # `custom_function` present but null, beside a real file.
        (
            "    custom_function: null\n    custom_function_file: m.py\n",
            {"m.py": b"x"},
        ),
        # A real function, beside an empty filename.
        (
            '    custom_function_file: ""\n'
            "    custom_function: |\n"
            "      def evaluate(instance): return 1.0\n",
            {},
        ),
    ],
)
def test_both_function_keys_are_refused_however_they_are_spelled(
    body: str, objects: dict[str, bytes]
) -> None:
    """`agents-cli` refuses the pair on key presence, so null or empty counts.

    Both keys are checked for presence regardless of truthiness values.
    """
    library = load_library(
        {
            "eval_config.yaml": (
                "metrics_to_run: [m]\ncustom_metrics:\n  - name: m\n" + body
            ).encode(),
            **objects,
        }
    )

    assert not library
    (name, why) = library.failures[0]
    assert name == "m"
    assert "both custom_function and custom_function_file" in why


@pytest.mark.parametrize("name", ["123", "[a]", "{k: v}", "null"])
def test_a_metric_name_that_is_not_a_string_costs_only_itself(
    name: str,
) -> None:
    """A YAML name of the wrong type reached `str` helpers and raised there.

    That is outside the per-metric guard, so one malformed entry took down the
    whole sweep -- and the window only advances on a completed run, so every
    later sweep died on the same config.
    """
    library = load_library(
        {
            "eval_config.yaml": (
                f"metrics_to_run: [{name}, ok]\n"
                "custom_metrics:\n"
                f"  - name: {name}\n"
                "    custom_function: |\n"
                "      def evaluate(instance): return {'score': 1.0}\n"
                "  - name: ok\n"
                "    custom_function: |\n"
                "      def evaluate(instance): return {'score': 1.0}\n"
            ).encode()
        }
    )

    assert list(library) == ["ok"], (
        "the well-formed metric beside it must survive"
    )


def test_a_function_beside_a_prompt_template_runs_as_agents_cli_runs_it() -> (
    None
):
    """`agents-cli` reaches its `custom_function` branch before it ever looks at
    `prompt_template`, so an entry carrying both runs the function there.

    Judged first here meant the same entry scored nothing and said nothing.
    """
    library = load_library(
        _config(
            "metrics_to_run: [m]\n"
            "custom_metrics:\n"
            "  - name: m\n"
            '    prompt_template: "is this good?"\n'
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
        )
    )

    assert list(library) == ["m"]
    assert not library.judged


@pytest.mark.parametrize(
    ("label", "body"),
    [
        (
            "rubric group on a judge",
            '    rubric_group_name: g\n    prompt_template: "j"\n',
        ),
        ("no body at all", ""),
    ],
)
def test_a_config_agents_cli_refuses_is_named_not_silently_skipped(
    label: str, body: str
) -> None:
    """These raise under `agents-cli eval run`. Skipped at INFO instead, they
    never reached `library.failures`, so the run summary reported a clean sweep
    that had in fact scored nothing."""
    library = load_library(
        _config("metrics_to_run: [m]\ncustom_metrics:\n  - name: m\n" + body)
    )

    assert not library
    assert [name for name, _why in library.failures] == ["m"], label


def test_a_judged_metric_with_a_stray_execution_loads_rather_than_failing() -> (
    None
):
    """`agents-cli` only reads `execution` for a metric carrying a function, so
    a stray one on a judge is inert there and inert here."""
    library = load_library(
        _config(
            "metrics_to_run: [m]\n"
            "custom_metrics:\n"
            "  - name: m\n"
            '    prompt_template: "j"\n'
            "    execution: bogus\n"
        )
    )

    assert not library
    assert not library.failures
    assert [m.name for m in library.judged] == ["m"]


def test_a_remote_function_is_not_refused_for_its_execution_value() -> None:
    """`agents-cli` reads `execution` only for a function it would run itself.

    It inlines `custom_function_file` into `custom_function` and reads the key
    inside that branch, so a standalone `remote_custom_function` never has its
    `execution` looked at. Refusing one here would reject a config that tool
    accepts and runs.
    """
    library = load_library(
        _config(
            "metrics_to_run: [m]\n"
            "custom_metrics:\n"
            "  - name: m\n"
            "    execution: bogus\n"
            "    remote_custom_function: |\n"
            "      def evaluate(instance): return 1.0\n"
        )
    )

    assert not library.failures


@pytest.mark.parametrize("name", ["bleu", "rouge1", "exact_match"])
def test_a_bare_sdk_computed_name_parameterizes_it(name: str) -> None:
    """`exact_match`, `bleu` and `rouge*` are dispatched on the name alone.

    They are absent from the predefined registry, so a name-only resolver reads
    them as undefined and refuses an entry that `agents-cli` accepts.
    """
    library = load_library(
        _config(
            f"metrics_to_run: [{name}]\ncustom_metrics:\n  - name: {name}\n"
        )
    )

    assert not library.failures


def test_a_reserved_name_is_refused_only_against_a_definition() -> None:
    """An entry with no body is parameterizing the built-in it names, which is
    the ordinary way to configure one. Reserved only bites a definition that
    the service would ignore in favour of its own."""
    bare = load_library(
        _config(
            "metrics_to_run: [safety_v1]\ncustom_metrics:\n  - name: safety_v1\n"
        )
    )
    defined = load_library(
        _config(
            "metrics_to_run: [safety_v1]\n"
            "custom_metrics:\n"
            "  - name: safety_v1\n"
            "    custom_function: |\n"
            "      def evaluate(instance): return {'score': 1.0}\n"
        )
    )

    assert not bare.failures
    assert [n for n, _why in defined.failures] == ["safety_v1"]


def test_an_inert_execution_value_warns_rather_than_refusing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both tools run a `remote_custom_function` entry on the eval service and
    ignore `execution`, so refusing would reject a config that works in both.

    It is still almost always a typo, and nothing else would mention it.
    """
    with caplog.at_level(logging.WARNING):
        library = load_library(
            _config(
                "metrics_to_run: [m]\n"
                "custom_metrics:\n"
                "  - name: m\n"
                "    execution: bogus\n"
                "    remote_custom_function: |\n"
                "      def evaluate(instance): return 1.0\n"
            )
        )

    assert not library.failures
    assert "is ignored" in caplog.text
