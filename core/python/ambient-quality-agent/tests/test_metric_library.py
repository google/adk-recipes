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

"""Tests for loading a code-metric library. Dict in, dict out: no filesystem."""

from __future__ import annotations

import json
import sys

import pytest
from ambient_quality_agent.tools.metrics.library import (
    PREDEFINED_KIND,
    load_library,
)

_CONFIG = b"""\
metrics_to_run:
  - turn_count
custom_metrics:
  - name: turn_count
    custom_function_file: turn_count.py
"""

_METRIC = b"""\
EXPECTED = "The conversation stays under ten turns."


def evaluate(instance):
    turns = (instance.get("agent_data") or {}).get("turns", [])
    return {"score": 1.0 if len(turns) < 10 else 0.0}
"""


def _library(
    config: bytes = _CONFIG, metric: bytes = _METRIC
) -> dict[str, bytes]:
    return {"eval_config.yaml": config, "turn_count.py": metric}


def test_loads_the_selected_metric() -> None:
    library = load_library(_library())

    assert set(library) == {"turn_count"}
    metric = library["turn_count"]
    assert metric.expected == "The conversation stays under ten turns."
    assert metric.evaluate({"agent_data": {"turns": [1, 2]}}) == {"score": 1.0}


def test_a_defined_metric_absent_from_metrics_to_run_is_ignored() -> None:
    assert (
        load_library(
            _library(config=_CONFIG.replace(b"  - turn_count\n", b"  []\n", 1))
        )
        == {}
    )


def test_a_name_repeated_in_metrics_to_run_loads_once() -> None:
    config = _CONFIG.replace(
        b"  - turn_count\n", b"  - turn_count\n  - turn_count\n", 1
    )

    assert list(load_library(_library(config=config))) == ["turn_count"]


def test_no_config_yields_an_empty_library() -> None:
    assert load_library({"turn_count.py": _METRIC}) == {}


@pytest.mark.parametrize("suffix", ["yaml", "yml", "json"])
def test_every_config_extension_agents_cli_accepts(suffix: str) -> None:
    body = (
        b'{"metrics_to_run": ["turn_count"], "custom_metrics":'
        b' [{"name": "turn_count", "custom_function_file": "turn_count.py"}]}'
        if suffix == "json"
        else _CONFIG
    )

    assert sorted(
        load_library({f"eval_config.{suffix}": body, "turn_count.py": _METRIC})
    ) == ["turn_count"]


def test_a_module_without_evaluate_is_recorded_as_a_failure() -> None:
    library = load_library(_library(metric=b"EXPECTED = 'x'\n"))

    assert library == {}
    assert [name for name, _ in library.failures] == ["turn_count"]
    assert "evaluate" in library.failures[0][1]


@pytest.mark.parametrize(
    "metric",
    [
        b"def evaluate(instance):\n    return 1.0\n",
        b"EXPECTED = '  '\ndef evaluate(i):\n    return 1.0\n",
    ],
)
def test_a_metric_without_expected_still_loads(metric: bytes) -> None:
    """`agents-cli` has no EXPECTED, so requiring one would make every metric
    written for it unusable here."""
    library = load_library(_library(metric=metric))

    assert (
        library["turn_count"].expected
        == "The agent satisfies the 'turn_count' metric."
    )


def test_a_metric_naming_an_unpublished_file_is_recorded_as_a_failure() -> None:
    library = load_library({"eval_config.yaml": _CONFIG})

    assert library == {}
    assert "not published" in library.failures[0][1]


def test_a_config_that_is_not_a_mapping_is_an_error() -> None:
    with pytest.raises(ValueError, match="mapping"):
        load_library({"eval_config.yaml": b"- just\n- a\n- list\n"})


def test_an_inline_custom_function_loads() -> None:
    config = (
        b"metrics_to_run: [inline]\ncustom_metrics:\n  - name: inline\n"
        b"    custom_function: |\n"
        b"      EXPECTED = 'The agent is brief.'\n"
        b"      def evaluate(instance):\n"
        b"          return {'score': 1.0}\n"
    )

    library = load_library({"eval_config.yaml": config})

    assert library["inline"].expected == "The agent is brief."
    assert library["inline"].evaluate({}) == {"score": 1.0}


def test_declaring_both_a_file_and_inline_source_is_an_error() -> None:
    config = (
        b"metrics_to_run: [x]\ncustom_metrics:\n  - name: x\n"
        b"    custom_function_file: x.py\n    custom_function: 'def evaluate(i): pass'\n"
    )

    library = load_library({"eval_config.yaml": config})

    assert library == {}
    assert "both" in library.failures[0][1]


@pytest.mark.parametrize(
    "entry",
    [
        b"    remote_custom_function: gs://b/x.py\n",
        b"    threshold: 0.5\n",
    ],
)
def test_an_entry_this_producer_cannot_run_is_skipped(entry: bytes) -> None:
    """A config mixing remote, bare and local metrics must still yield its
    local ones; failing the lot would make agents-cli configs unusable."""
    config = (
        b"metrics_to_run:\n  - other\n  - turn_count\n"
        b"custom_metrics:\n  - name: other\n"
        + entry
        + b"  - name: turn_count\n    custom_function_file: turn_count.py\n"
    )

    assert sorted(load_library(_library(config=config))) == ["turn_count"]


def test_a_prompt_template_beside_a_local_function_still_yields_the_local_one() -> (
    None
):
    """The judged entry loads into its own bucket without displacing its peer."""
    config = (
        b"metrics_to_run:\n  - other\n  - turn_count\n"
        b"custom_metrics:\n  - name: other\n"
        b"    prompt_template: 'Did it cite a source?'\n"
        b"  - name: turn_count\n    custom_function_file: turn_count.py\n"
    )

    library = load_library(_library(config=config))

    assert sorted(library) == ["turn_count"]
    assert [m.name for m in library.judged] == ["other"]


def test_a_name_with_no_custom_definition_is_skipped() -> None:
    """In a real config an unmatched name is a predefined metric, meant for a
    different runner -- not a mistake to fail the library over."""
    assert (
        load_library(
            {
                "eval_config.yaml": b"metrics_to_run: [safety]\ncustom_metrics: []\n"
            }
        )
        == {}
    )


def test_a_metric_may_define_a_dataclass() -> None:
    """`dataclasses` resolves the defining module through `sys.modules`, which
    is why the source is executed as a module rather than in a bare dict."""
    metric = (
        b"import dataclasses\nEXPECTED = 'x'\n"
        b"@dataclasses.dataclass\nclass R:\n    n: int\n"
        b"def evaluate(i):\n    return {'score': float(R(1).n)}\n"
    )

    assert load_library(_library(metric=metric))["turn_count"].evaluate({}) == {
        "score": 1.0
    }


def test_two_libraries_in_one_process_do_not_collide() -> None:
    first = load_library(_library(metric=_METRIC.replace(b"1.0", b"0.25")))
    second = load_library(_library())

    assert first["turn_count"].evaluate({}) == {"score": 0.25}
    assert second["turn_count"].evaluate({}) == {"score": 1.0}


def test_a_module_that_fails_to_execute_leaves_no_trace_in_sys_modules() -> (
    None
):
    before = set(sys.modules)

    library = load_library(_library(metric=b"EXPECTED = 'x'\n1 / 0\n"))

    assert "ZeroDivisionError" in library.failures[0][1]
    assert set(sys.modules) == before


# --- metrics that call a model ---------------------------------------------


def _judge_config() -> dict[str, bytes]:
    return {
        "eval_config.yaml": b"""
metrics_to_run: [judge, plain]
custom_metrics:
  - name: judge
    custom_function_file: judge.py
  - name: plain
    custom_function_file: plain.py
""",
        "judge.py": b"from google import genai\n\ndef evaluate(instance):\n    return 1.0\n",
        "plain.py": b"def evaluate(instance):\n    return 1.0\n",
    }


def test_a_metric_that_imports_a_model_client_is_flagged() -> None:
    """The config cannot show this: the call is inside the metric, so the
    entry looks like any other local function."""
    library = load_library(_judge_config())

    assert library["judge"].model_modules == ("google.genai",)
    assert library["plain"].model_modules == ()


def test_the_flag_does_not_stop_the_metric_running() -> None:
    """An author may mean it. The point is that it is visible, not vetoed."""
    library = load_library(_judge_config())

    assert library["judge"].evaluate({}) == pytest.approx(1.0)


def test_a_plain_import_of_a_model_client_is_flagged_too() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [m]\ncustom_metrics:\n"
        b"  - name: m\n    custom_function_file: m.py\n",
        "m.py": b"import vertexai.preview\n\ndef evaluate(instance):\n    return 1.0\n",
    }

    assert load_library(objects)["m"].model_modules == ("vertexai",)


def test_an_unrelated_import_is_not_flagged() -> None:
    objects = {
        "eval_config.yaml": b"metrics_to_run: [m]\ncustom_metrics:\n"
        b"  - name: m\n    custom_function_file: m.py\n",
        "m.py": b"import json\nfrom collections import Counter\n\n"
        b"def evaluate(instance):\n    return 1.0\n",
    }

    assert load_library(objects)["m"].model_modules == ()


def test_each_declined_entry_carries_the_kind_it_would_have_been_scored_by() -> (
    None
):
    """Classifies declined entries as predefined; a judge prompt is no longer
    declined, with or without a stray `execution`."""
    config = json.dumps(
        {
            "metrics_to_run": [
                "unlisted",
                "judged",
                "bleu",
                "judge_remote",
                "safety",
            ],
            "custom_metrics": [
                {"name": "judged", "prompt_template": "is it polite?"},
                {"name": "bleu"},
                {
                    "name": "judge_remote",
                    "prompt_template": "is it polite?",
                    "execution": "remote",
                },
                {"name": "safety", "execution": "remote"},
            ],
        }
    ).encode()

    library = load_library({"eval_config.json": config})

    assert {name: kind for name, _reason, kind in library.declined} == {
        "unlisted": PREDEFINED_KIND,
        "bleu": PREDEFINED_KIND,
        "safety": PREDEFINED_KIND,
    }
    assert sorted(m.name for m in library.judged) == ["judge_remote", "judged"]
