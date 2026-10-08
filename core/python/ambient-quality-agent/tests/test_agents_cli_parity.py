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

"""Hold this repo's two metric validators to what `agents-cli` actually does.

The eval config format is `agents-cli`'s, and this repo reads it twice: the
loader the deployed agent runs (`tools.metrics.library`) and the validator
`aqua metrics publish` runs on the operator's machine
(`ambient_quality_cli.metrics`). Both restate `agents-cli`'s rules by hand.

Neither can call `agents-cli` to avoid that. The agent does not ship it --
`google-agents-cli` is not in `src/ambient_quality_agent/requirements.txt` --
and there is no validation entry point to call in any case: the nearest thing,
`prepare_eval_metrics`, is reached only from `eval grade` and `eval submit`,
and it compiles and executes every local `custom_function` as it builds the
metric list. Publishing must not run the library it is publishing.

So the rules stay duplicated and this pins the duplication instead. Each case
below states what `agents-cli` does with a config, asserts that against the
installed `agents-cli`, and asserts this repo agrees. A rule that changes
upstream fails here rather than in a sweep.

`aqua run` leans on `agents-cli` too: it hands the turn to
`agents-cli run --mode a2a`, so the options it passes are pinned here as well.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_agent.tools.metrics.library import (
    PREDEFINED_KIND,
    load_library,
)
from ambient_quality_cli import metrics as cli_metrics

pytestmark = pytest.mark.parity

_FN = "def evaluate(instance):\n    return 1.0\n"
_MAPPING_FN = "def evaluate(instance):\n    return {'score': 1.0}\n"
_JUDGE_MODEL = (
    "projects/p/locations/l/publishers/google/models/gemini-2.5-flash"
)


def _acli_accepts(tmp_path: Path, entry: dict[str, Any]) -> bool:
    """Determines whether `agents-cli` builds a metric for `entry` without raising.

    Driven through `prepare_eval_metrics`, the function called by its own
    `eval grade`, to track real behavior.

    Args:
        tmp_path: Temporary directory fixture for generating test config.
        entry: Metric entry configuration dictionary.

    Returns:
        True if `agents-cli` accepts the metric entry without error.
    """
    import click
    from google.agents.cli.eval.eval_utils import prepare_eval_metrics

    config = tmp_path / "eval_config.json"
    config.write_text(
        json.dumps(
            {"metrics_to_run": [entry["name"]], "custom_metrics": [entry]}
        )
    )
    try:
        metrics, _local, _remote = prepare_eval_metrics(str(config), None)
    except click.ClickException:
        return False
    # A name it passes through unresolved is a predefined metric, not this
    # entry's definition being accepted.
    return any(not isinstance(m, str) for m in metrics)


def _we_accept(entry: dict[str, Any]) -> bool:
    """Determines whether this repository accepts and runs `entry` via either loader.

    Args:
        entry: Metric entry configuration dictionary.

    Returns:
        True if the metric entry is accepted by the local or remote loader.
    """
    objects = {
        "eval_config.json": json.dumps(
            {"metrics_to_run": [entry["name"]], "custom_metrics": [entry]}
        ).encode()
    }
    library = load_library(objects)
    return bool(library) or bool(library.remote) or bool(library.judged)


# `expected` is what `agents-cli` does, asserted against it rather than trusted.
_CASES: list[tuple[str, dict[str, Any], bool]] = [
    ("plain local function", {"name": "m", "custom_function": _FN}, True),
    (
        "explicit local execution",
        {"name": "m", "custom_function": _FN, "execution": "local"},
        True,
    ),
    (
        "remote execution",
        {"name": "m", "custom_function": _FN, "execution": "remote"},
        True,
    ),
    (
        "standalone remote function",
        {"name": "m", "remote_custom_function": _FN},
        True,
    ),
    (
        "a function beside a judge prompt runs the function",
        {"name": "m", "custom_function": _FN, "prompt_template": "judge"},
        True,
    ),
    (
        "capitalised execution is refused",
        {"name": "m", "custom_function": _FN, "execution": "Remote"},
        False,
    ),
    (
        "unknown execution is refused",
        {"name": "m", "custom_function": _FN, "execution": "elsewhere"},
        False,
    ),
    (
        # `agents-cli` reads `execution` only inside its `custom_function`
        # branch, so a standalone remote function never has the key looked at.
        "a remote function ignores its execution value",
        {"name": "m", "remote_custom_function": _FN, "execution": "elsewhere"},
        True,
    ),
    (
        "rubric_group_name on a custom metric is refused",
        {"name": "m", "custom_function": _FN, "rubric_group_name": "g"},
        False,
    ),
    (
        "a reserved name is refused",
        {"name": "safety_v1", "custom_function": _FN},
        False,
    ),
    (
        "both function keys are refused",
        {"name": "m", "custom_function": _FN, "custom_function_file": "m.py"},
        False,
    ),
    ("an entry with no body is refused", {"name": "m"}, False),
    ("a judge prompt alone", {"name": "m", "prompt_template": "judge"}, True),
    (
        "a judge prompt with a judge model",
        {"name": "m", "prompt_template": "judge", "judge_model": _JUDGE_MODEL},
        True,
    ),
    (
        "a judge prompt with a bare judge model id, as the agents-cli guide writes it",
        {
            "name": "m",
            "prompt_template": "judge",
            "judge_model": "gemini-3.8-flash",
        },
        True,
    ),
    (
        "a blank judge prompt is refused",
        {"name": "m", "prompt_template": "  "},
        False,
    ),
    (
        "an out-of-range sampling count is refused",
        {
            "name": "m",
            "prompt_template": "judge",
            "judge_model_sampling_count": 99,
        },
        False,
    ),
]

_PARAMETERIZING_A_BUILTIN = [
    # Bare entries naming something the service already knows. `agents-cli`
    # accepts them and so must we -- but this repo's code-metric loader does
    # not *run* them, because they carry no function for it to run. The eval
    # node handles them by name. So the claim is that they are not refused,
    # which is a different thing from the run-parity table above.
    ("a registered built-in", {"name": "safety_v1"}),
    ("sdk-computed: bleu", {"name": "bleu"}),
    ("sdk-computed: rouge1", {"name": "rouge1"}),
    ("sdk-computed: exact_match", {"name": "exact_match"}),
]


@pytest.mark.parametrize(
    "entry",
    [pytest.param(e, id=label) for label, e in _PARAMETERIZING_A_BUILTIN],
)
def test_parameterizing_a_builtin_is_declined_not_refused(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    """Declined, not refused -- and the difference is the operator's experience.

    A refused metric is a config error named in the run summary. One declined
    here is simply not this loader's to run, and reaching the summary as a
    failure would tell the operator to fix a config that is already correct.
    """
    assert _acli_accepts(tmp_path, entry) is True

    objects = {
        "eval_config.json": json.dumps(
            {"metrics_to_run": [entry["name"]], "custom_metrics": [entry]}
        ).encode()
    }
    library = load_library(objects)

    assert not library.failures, "declined, so nothing to report as broken"
    assert not library and not library.remote, (
        "and not run by the code-metric path"
    )
    assert [kind for _name, _reason, kind in library.declined] == [
        PREDEFINED_KIND
    ]


@pytest.mark.parametrize(
    ("entry", "agents_cli_accepts"),
    [pytest.param(e, a, id=label) for label, e, a in _CASES],
)
def test_agents_cli_still_behaves_as_this_repo_assumes(
    tmp_path: Path, entry: dict[str, Any], agents_cli_accepts: bool
) -> None:
    """The upstream half. Fails when `agents-cli` changes a rule under us."""
    assert _acli_accepts(tmp_path, entry) is agents_cli_accepts


@pytest.mark.parametrize(
    ("entry", "agents_cli_accepts"),
    [pytest.param(e, a, id=label) for label, e, a in _CASES],
)
def test_this_repo_runs_what_agents_cli_runs(
    entry: dict[str, Any], agents_cli_accepts: bool
) -> None:
    """The local half, against the same table.

    Accepting what `agents-cli` refuses runs a config the operator's own tool
    rejects. Refusing what it accepts drops a metric they have every reason to
    expect, and the sweep scores less than they published.
    """
    assert _we_accept(entry) is agents_cli_accepts


_MULTI_FAULT = [
    # Each of these breaks more than one rule. `agents-cli` checks them in a
    # fixed order, so which fault it names is deterministic -- and an operator
    # told to fix a different one by each tool would reasonably think one is
    # wrong. The reason has to match, not just the refusal.
    (
        "rubric group outranks a bad execution",
        {
            "name": "m",
            "custom_function": _FN,
            "execution": "bogus",
            "rubric_group_name": "g",
        },
        "rubric_group_name",
    ),
    (
        "both function keys outrank a bad execution",
        {
            "name": "m",
            "custom_function": _FN,
            "custom_function_file": "m.py",
            "execution": "bogus",
        },
        "custom_function_file",
    ),
    (
        "a reserved name outranks a bad execution",
        {"name": "safety_v1", "custom_function": _FN, "execution": "bogus"},
        "reserved",
    ),
]


@pytest.mark.parametrize(
    ("entry", "names_rule"),
    [pytest.param(e, r, id=label) for label, e, r in _MULTI_FAULT],
)
def test_a_config_with_several_faults_is_refused_for_the_same_one(
    tmp_path: Path, entry: dict[str, Any], names_rule: str
) -> None:
    """Both tools refuse these. This pins *which* fault each one reports."""
    assert _acli_accepts(tmp_path, entry) is False

    objects = {
        "eval_config.json": json.dumps(
            {"metrics_to_run": [entry["name"]], "custom_metrics": [entry]}
        ).encode()
    }
    library = load_library(objects)

    assert library.failures, "refused, so it must be named"
    (_name, why) = library.failures[0]
    assert names_rule in why, (
        f"reported {why!r}, expected the {names_rule} rule"
    )


def test_the_publish_validator_agrees_with_the_agent_loader(
    tmp_path: Path,
) -> None:
    """And the two halves of this repo agree with each other.

    `aqua metrics publish` refusing what the sweep would run, or passing what it
    would refuse, is the same divergence one step earlier.
    """
    for label, entry, _acli in _CASES:
        objects = {
            "eval_config.json": json.dumps(
                {"metrics_to_run": [entry["name"]], "custom_metrics": [entry]}
            ).encode()
        }
        try:
            published = bool(cli_metrics.validate(objects).metrics)
        except cli_metrics.ValidationError:
            published = False
        assert published is _we_accept(entry), label


_TERNARY_MAPPING_FN = (
    "def evaluate(instance):\n    return {'score': 1.0} if instance else 0.0\n"
)
_ASYNC_FN = "async def evaluate(instance):\n    return 1.0\n"

# The rules this repo adds rather than mirrors, each because the eval service
# behaves in a way `agents-cli` does not check for. Every one of them is refused
# on the remote path and allowed on the local one, where the loader runs the
# function itself and none of the three applies.
_DELIBERATE_DIFFERENCES = [
    ("a visible score mapping", {"custom_function": _MAPPING_FN}),
    (
        "a score mapping in one arm of a ternary",
        {"custom_function": _TERNARY_MAPPING_FN},
    ),
    ("an async evaluate", {"custom_function": _ASYNC_FN}),
    (
        "a non-finite threshold",
        {"custom_function": _FN, "threshold": float("nan")},
    ),
]


@pytest.mark.parametrize(
    "extra", [pytest.param(e, id=label) for label, e in _DELIBERATE_DIFFERENCES]
)
def test_a_remote_only_rule_is_refused_remotely_and_allowed_locally(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    """`agents-cli` accepts all of these, so each is a deliberate difference.

    Refusing one locally too would block a metric the loader would have run --
    the opposite failure, and just as invisible until an operator hits it.
    """
    remote = {"name": "m", **extra}
    remote["remote_custom_function"] = remote.pop("custom_function")
    local = {"name": "m", **extra}

    assert _acli_accepts(tmp_path, remote) is True
    assert _we_accept(remote) is False, "refused here, by design"
    assert _we_accept(local) is True, "and only for the remote side"


@pytest.mark.parametrize(
    "extra", [pytest.param(e, id=label) for label, e in _DELIBERATE_DIFFERENCES]
)
def test_publish_applies_the_remote_only_rules_exactly_as_the_loader_does(
    extra: dict[str, Any],
) -> None:
    """These rules live twice, so they are the ones that drift.

    A shape the validator passes and the loader refuses is published and then
    declines to score. One the validator refuses and the loader would run
    cannot be published at all. Both halves are checked here.
    """
    remote = {"name": "m", **extra}
    remote["remote_custom_function"] = remote.pop("custom_function")

    for entry in (remote, {"name": "m", **extra}):
        objects = {
            "eval_config.json": json.dumps(
                {"metrics_to_run": [entry["name"]], "custom_metrics": [entry]}
            ).encode()
        }
        try:
            published = bool(cli_metrics.validate(objects).metrics)
        except cli_metrics.ValidationError:
            published = False
        assert published is _we_accept(entry), entry


# The judge-side rules this repo adds, on the same grounds as the four above:
# `agents-cli` builds each of these, and the eval service was measured to
# error every session on it, so a sweep would score nothing and stall.
_JUDGE_DELIBERATE_DIFFERENCES = [
    (
        "{agent_eval_data} in the template",
        {"prompt_template": "{agent_eval_data}"},
    ),
    (
        "return_raw_output",
        {"prompt_template": "judge", "return_raw_output": True},
    ),
]


@pytest.mark.parametrize(
    "extra",
    [pytest.param(e, id=label) for label, e in _JUDGE_DELIBERATE_DIFFERENCES],
)
def test_a_judge_shape_agents_cli_builds_is_refused_here_by_design(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    entry = {"name": "m", **extra}
    objects = {
        "eval_config.json": json.dumps(
            {"metrics_to_run": ["m"], "custom_metrics": [entry]}
        ).encode()
    }

    assert _acli_accepts(tmp_path, entry) is True
    assert _we_accept(entry) is False, "refused here, by design"
    with pytest.raises(cli_metrics.ValidationError):
        cli_metrics.validate(objects)


def test_agents_cli_run_accepts_the_options_aqua_run_passes() -> None:
    """`aqua run` builds an `agents-cli run` argv; a renamed or dropped option
    upstream would fail every turn with a usage error."""
    import click
    from google.agents.cli.run.cmd_run import cmd_run

    options = {
        opt: param
        for param in cmd_run.params
        if isinstance(param, click.Option)
        for opt in param.opts
    }

    assert {
        "--url",
        "--app-name",
        "--session-id",
        "--file",
    } <= set(options)
    assert options["--file"].multiple
    mode = options["--mode"].type
    assert isinstance(mode, click.Choice)
    assert "a2a" in mode.choices
