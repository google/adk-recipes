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

"""Tests for the agents-cli extension: its manifest and the extension/ wrappers.

The wrappers only ever shell out, so every test here replaces `_acli.run` with a
recorder and asserts on the argv that would have been executed. That is the part
worth pinning: a wrapper that builds the wrong vector fails against a real
project minutes later, in a message about Terraform or Agent Runtime rather than
about the wrapper.

The manifest tests are the cheap half and catch the likeliest breakage of all --
renaming or moving a script without editing the `run:` vector that names it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import _acli
import acli_aqua
import acli_deploy
import acli_infra
import acli_ui
import pytest
import yaml
from ambient_quality_shared import terraform_state

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "agents-cli-extension.yaml"

# Expected dashboard Cloud Run service name for the test project below.
UI_SERVICE = "it-support-agent-aqua-ui"

# Mock path for the staged build context in deploy tests.
STAGED_CONTEXT = Path("/staged/ui-build")

# Ignore pattern keeping `.aqua/` out of git and container uploads.
IGNORE_ENTRY = "/.aqua/"

_INFO = {
    "project_root": "/proj",
    "project_name": "it-support-agent",
    "language": "python",
    "deployment_target": "agent_runtime",
    "agent_directory": "app",
    "region": "us-east1",
}


class Recorder:
    """Stand-in for `_acli.run` that records argv and replays canned results."""

    def __init__(
        self, results: dict[str, tuple[int, str]] | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._results = results or {}

    def __call__(
        self, argv, *, cwd=None, env=None, capture=False, check=False
    ) -> subprocess.CompletedProcess:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": None if cwd is None else str(cwd),
                "env": env,
            }
        )
        for marker, (code, stdout) in self._results.items():
            if marker in argv:
                return subprocess.CompletedProcess(list(argv), code, stdout, "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def argv_containing(self, token: str) -> list[str]:
        for call in self.calls:
            if token in call["argv"]:
                return call["argv"]
        raise AssertionError(
            f"no recorded call contains {token!r}: {self.calls}"
        )


@pytest.fixture
def wrappers(monkeypatch, tmp_path):
    """Point every wrapper at a fake project and a recording `run`.

    Each wrapper imports the helpers by name, so a helper it calls directly has
    to be replaced on the wrapper's own module; `run` is the exception, since
    `run_builtin` reaches it through `_acli`'s globals.
    """
    # Simulates `gcloud run services list` finding an existing dashboard service.
    recorder = Recorder({"list": (0, f"{UI_SERVICE}\n")})
    info = dict(_INFO, project_root=str(tmp_path))
    for module in (_acli, acli_infra, acli_deploy, acli_ui):
        monkeypatch.setattr(module, "run", recorder, raising=False)
        monkeypatch.setattr(
            module,
            "resolve_agents_cli_path",
            lambda: "/bin/agents-cli",
            raising=False,
        )
        monkeypatch.setattr(
            module, "resolve_gcloud_path", lambda: "/bin/gcloud", raising=False
        )
        monkeypatch.setattr(
            module, "load_project_info", lambda: info, raising=False
        )
        monkeypatch.setattr(
            module, "resolve_extension_root", lambda: REPO_ROOT, raising=False
        )
    # Bypass staging `ui/` in tests that only verify command invocation.
    monkeypatch.setattr(
        acli_ui,
        "build_ui_staging_dir_for_deployment",
        lambda root, state_root: STAGED_CONTEXT,
    )
    # Manifest for project root discovery.
    (tmp_path / _acli.MANIFEST_FILE).write_text(
        "name: it-support-agent\n", "utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    # The fallback for `--observed-*`, so a developer's shell must not decide
    # what the tests see.
    for name in (
        "TF_VAR_observed_deployment_name",
        "TF_VAR_observed_agent_name",
    ):
        monkeypatch.delenv(name, raising=False)
    return recorder


@pytest.fixture
def own_checkout(wrappers, monkeypatch, tmp_path):
    """AQuA's own checkout, where the project and the extension are one tree."""
    for tree in (acli_infra.BOOTSTRAP_ROOT, acli_infra.DEPLOYMENT_ROOT):
        (tmp_path / tree).mkdir(parents=True)
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    for module in (_acli, acli_infra, acli_deploy, acli_ui):
        monkeypatch.setattr(module, "resolve_extension_root", tmp_path.resolve)
    return wrappers


# --- the manifest -----------------------------------------------------------


def _manifest() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_declares_the_three_commands():
    commands = _manifest()["commands"]
    assert set(commands["override"]) == {"deploy", "infra.single-project"}
    assert set(commands["add"]) == {"aqua"}


def test_every_run_vector_names_a_file_that_exists():
    """A `run:` token that is a path must resolve, or the command dies on use."""
    commands = _manifest()["commands"]
    entries = [*commands["override"].values(), *commands["add"].values()]
    paths = [tok for entry in entries for tok in entry["run"] if "/" in tok]
    assert paths, "expected each command to run a script"
    for path in paths:
        assert (REPO_ROOT / path).is_file(), (
            f"{path} named in the manifest is missing"
        )


def test_run_vectors_start_with_an_interpreter():
    """A bare script relies on a shebang and never runs on Windows."""
    commands = _manifest()["commands"]
    for entry in [*commands["override"].values(), *commands["add"].values()]:
        assert not entry["run"][0].endswith(".py")


def test_terraform_roots_declare_a_local_backend():
    """Without the block, `-backend-config=path=` is accepted and ignored."""
    for root in (acli_infra.BOOTSTRAP_ROOT, acli_infra.DEPLOYMENT_ROOT):
        versions = (REPO_ROOT / root / "versions.tf").read_text(
            encoding="utf-8"
        )
        assert 'backend "local" {}' in versions


# --- argv handling ----------------------------------------------------------


def test_take_flag_removes_only_that_flag():
    remaining, taken = _acli.take_flag(
        ["--apply", "--skip-aqua", "-x"], "--skip-aqua"
    )
    assert (remaining, taken) == (["--apply", "-x"], True)
    assert _acli.take_flag(["--apply"], "--skip-aqua") == (["--apply"], False)


@pytest.mark.parametrize(
    "argv",
    [
        ["--observed-agent-name", "a1", "--apply"],
        ["--observed-agent-name=a1", "--apply"],
    ],
)
def test_take_option_removes_both_spellings(argv):
    assert _acli.take_option(argv, "--observed-agent-name") == (
        ["--apply"],
        "a1",
    )


def test_take_option_drops_a_trailing_option_carrying_nothing():
    """The built-in would reject what it cannot parse, value or no value."""
    argv = ["--apply", "--observed-agent-name"]
    assert _acli.take_option(argv, "--observed-agent-name") == (
        ["--apply"],
        None,
    )


def test_take_option_takes_every_occurrence_and_reads_the_first():
    argv = ["--observed-agent-name", "a1", "--observed-agent-name=a2"]
    assert _acli.take_option(argv, "--observed-agent-name") == ([], "a1")


@pytest.mark.parametrize(
    "argv",
    [["--project", "p2", "--apply"], ["--project=p2"]],
)
def test_option_value_reads_both_spellings(argv):
    assert _acli.extract_option_value(argv, "--project") == "p2"


def test_option_value_is_none_when_absent():
    assert _acli.extract_option_value(["--apply"], "--project") is None


def test_resolve_project_prefers_argv_over_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    assert _acli.resolve_project(["--project", "from-argv"]) == "from-argv"
    assert _acli.resolve_project([]) == "from-env"


def test_observed_agent_name_prefers_the_contract_over_the_convention():
    assert _acli.resolve_observed_agent_name(
        {**_INFO, "root_agent_name": "explicit"}
    ) == ("explicit")
    # The fallback re-derives the scaffold's own transformation.
    assert _acli.resolve_observed_agent_name(_INFO) == "it_support_agent"


def test_the_observed_project_answers_for_itself(monkeypatch):
    """The vendored install: the project in the working directory is the agent."""
    for name in (
        "TF_VAR_observed_deployment_name",
        "TF_VAR_observed_agent_name",
    ):
        monkeypatch.delenv(name, raising=False)
    options = _acli.ObservedOptions(None, None)
    assert _acli.resolve_observed_identity(
        options, _INFO, standalone=False
    ) == (
        "it-support-agent",
        "it_support_agent",
    )


def test_observed_options_fall_back_to_terraforms_own_variables(monkeypatch):
    """A deployment that exports them for `terraform` need not repeat them."""
    monkeypatch.setenv("TF_VAR_observed_deployment_name", "sherlock-v2")
    monkeypatch.setenv("TF_VAR_observed_agent_name", "from_env")
    remaining, options = _acli.take_observed_options(["--apply"])
    assert remaining == ["--apply"]
    assert options == ("sherlock-v2", "from_env")


def test_an_observed_flag_beats_terraforms_environment(monkeypatch):
    monkeypatch.setenv("TF_VAR_observed_agent_name", "from_env")
    _, options = _acli.take_observed_options(
        ["--observed-agent-name", "from_flag"]
    )
    assert options.agent_name == "from_flag"


@pytest.mark.parametrize(
    "argv",
    [
        ["--observed-agent-name"],
        ["--observed-agent-name", "--apply"],
        ["--observed-agent-name="],
        ["--observed-deployment-name="],
    ],
)
def test_an_observed_flag_without_a_value_is_refused(argv, capsys):
    """Falling through to the standalone default would deploy the wrong AQuA."""
    with pytest.raises(SystemExit):
        _acli.take_observed_options(argv)
    assert "needs a value" in capsys.readouterr().err


def test_aqua_service_name_matches_the_module():
    assert (
        _acli.build_aqua_service_name("it-support-agent")
        == "it-support-agent-aqua"
    )


def test_ui_service_name_matches_the_module():
    """Matches `<engine name>-ui` in `ui.tf`."""
    assert _acli.build_aqua_ui_service_name("it-support-agent") == UI_SERVICE


def test_telemetry_table_follows_the_observed_deployment_target():
    assert "reasoning_engine" in _acli.resolve_observed_telemetry_table(
        "agent_runtime"
    )
    assert _acli.resolve_observed_telemetry_table("cloud_run") == (
        "gen_ai_client_inference_operation_details"
    )


# --- infra ------------------------------------------------------------------


def test_infra_runs_the_builtin_first_and_forwards_argv(wrappers, monkeypatch):
    monkeypatch.setattr(acli_infra, "run_terraform", lambda *a, **k: None)
    acli_infra.main(["--project", "p2", "--apply"])
    assert wrappers.calls[0]["argv"] == [
        "/bin/agents-cli",
        "infra",
        "single-project",
        "--project",
        "p2",
        "--apply",
    ]


def test_infra_leaves_aqua_out_without_its_flag(wrappers, monkeypatch, capsys):
    """An agent attached to an AQuA deployed elsewhere gets no second one."""
    applied: list[Any] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda *a, **k: applied.append(a)
    )
    assert acli_infra.main(["--apply"]) == 0
    assert [call["argv"][1:] for call in wrappers.calls] == [
        ["infra", "single-project", "--apply"]
    ]
    assert applied == []
    err = capsys.readouterr().err
    assert "--apply-aqua" in err
    assert "agents-cli aqua attach --apply --aqua-resource" in err


def test_infra_strips_its_own_flag_before_the_builtin(wrappers, monkeypatch):
    applied: list[Any] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda *a, **k: applied.append(a)
    )
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", lambda *a, **k: None
    )
    acli_infra.main(["--apply", "--apply-aqua"])
    assert wrappers.calls[0]["argv"][1:] == [
        "infra",
        "single-project",
        "--apply",
    ]
    assert len(applied) == 2, "bootstrap and the deployment root"


def test_infra_passes_the_variables_each_root_declares(wrappers, monkeypatch):
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra,
        "run_terraform",
        lambda root, **kwargs: runs.append({"root": root, **kwargs}),
    )
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", lambda *a, **k: None
    )
    acli_infra.main(["--project", "p2", "--apply", "--apply-aqua"])

    bootstrap, deployment = runs
    # bootstrap declares project_id alone; a value for anything else is an error.
    assert bootstrap["tf_vars"] == {"project_id": "p2"}
    assert bootstrap["state_key"] == "bootstrap"
    assert deployment["tf_vars"] == {
        "region": "us-east1",
        "observed_deployment_name": "it-support-agent",
        "observed_agent_name": "it_support_agent",
        "telemetry_table": "aiplatform_googleapis_com_reasoning_engine_stdout",
        "project_id": "p2",
    }
    assert all(run["apply"] for run in runs)


def test_infra_passes_the_engine_when_one_exists(wrappers, monkeypatch):
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        acli_infra,
        "read_observed_engine_resource_name",
        lambda *a, **k: "projects/p2/locations/us-east1/reasoningEngines/7",
    )
    acli_infra.main(["--project", "p2", "--apply-aqua"])
    assert runs[1]["tf_vars"]["observed_engine_resource_name"] == (
        "projects/p2/locations/us-east1/reasoningEngines/7"
    )
    assert not runs[1]["apply"], "no --apply means plan"


def _infra_show(payload: str, code: int = 0) -> Recorder:
    """Returns a `run` recorder answering `agents-cli infra show --json` with `payload`.

    Args:
        payload: JSON response payload to return.
        code: Return code to simulate.

    Returns:
        A configured `Recorder` instance.
    """
    return Recorder({"show": (code, payload)})


def test_engine_resource_name_reads_the_infra_contract(monkeypatch):
    """`infra show` is what makes the engine id readable from outside."""
    recorder = _infra_show(
        json.dumps({"outputs": {"agent_runtime_resource_name": "7"}})
    )
    monkeypatch.setattr(_acli, "run", recorder)
    monkeypatch.setattr(
        _acli, "resolve_agents_cli_path", lambda: "/bin/agents-cli"
    )
    # Composed with the project *id*: AQuA matches audit logs, which never
    # carry a project number.
    assert acli_infra.read_observed_engine_resource_name("p2", "us-east1") == (
        "projects/p2/locations/us-east1/reasoningEngines/7"
    )
    assert recorder.argv_containing("show") == [
        "/bin/agents-cli",
        "infra",
        "show",
        "--json",
    ]


def test_engine_resource_name_rewrites_a_full_name(monkeypatch):
    full = "projects/123456/locations/us-east1/reasoningEngines/7"
    monkeypatch.setattr(
        _acli,
        "run",
        _infra_show(
            json.dumps({"outputs": {"agent_runtime_resource_name": full}})
        ),
    )
    monkeypatch.setattr(
        _acli, "resolve_agents_cli_path", lambda: "/bin/agents-cli"
    )
    assert acli_infra.read_observed_engine_resource_name("p2", "us-east1") == (
        "projects/p2/locations/us-east1/reasoningEngines/7"
    )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        # No state yet: `infra show` reports no outputs rather than failing.
        (json.dumps({"outputs": {}}), 0),
        # A target that creates no engine at all.
        (json.dumps({"outputs": {"service_account": "x@y"}}), 0),
        ("", 1),
        ("not json", 0),
    ],
)
def test_engine_resource_name_is_none_when_there_is_no_engine(
    payload, code, monkeypatch
):
    monkeypatch.setattr(_acli, "run", _infra_show(payload, code))
    monkeypatch.setattr(
        _acli, "resolve_agents_cli_path", lambda: "/bin/agents-cli"
    )
    assert (
        acli_infra.read_observed_engine_resource_name("p2", "us-east1") is None
    )


def test_engine_resource_name_needs_a_project_to_compose_one(monkeypatch):
    recorder = _infra_show(
        json.dumps({"outputs": {"agent_runtime_resource_name": "7"}})
    )
    monkeypatch.setattr(_acli, "run", recorder)
    assert (
        acli_infra.read_observed_engine_resource_name(None, "us-east1") is None
    )
    assert recorder.calls == [], "no project means nothing to ask about"


# --- infra --destroy --------------------------------------------------------


def _destroy_recorder(monkeypatch, tmp_path) -> list[dict[str, Any]]:
    """Sets up a state file for the fake project and records `run_terraform` calls.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Temporary directory fixture.

    Returns:
        A list capturing the arguments passed to `run_terraform`.
    """
    (tmp_path / _acli.STATE_DIR_NAME).mkdir(exist_ok=True)
    (tmp_path / _acli.STATE_DIR_NAME / "single-project.tfstate").write_text(
        "{}", encoding="utf-8"
    )
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra,
        "run_terraform",
        lambda root, **kwargs: runs.append({"root": root, **kwargs}),
    )
    return runs


def test_destroy_never_runs_the_builtin(wrappers, monkeypatch, tmp_path):
    """There is no built-in teardown, so the agent's own infra is left alone."""
    runs = _destroy_recorder(monkeypatch, tmp_path)
    assert acli_infra.main(["--destroy", "--apply", "--project", "p2"]) == 0
    assert wrappers.calls == []
    assert [run["state_key"] for run in runs] == ["single-project"]


def test_destroy_leaves_the_bootstrap_root_standing(
    wrappers, monkeypatch, tmp_path
):
    """Its APIs are project-scoped and shared with everything else there."""
    runs = _destroy_recorder(monkeypatch, tmp_path)
    acli_infra.main(["--destroy", "--apply"])
    assert all(run["root"].name == "single-project" for run in runs)


def test_destroy_passes_only_the_variables_the_root_requires(
    wrappers, monkeypatch, tmp_path
):
    runs = _destroy_recorder(monkeypatch, tmp_path)
    acli_infra.main(["--destroy", "--apply", "--project", "p2"])
    (destroy,) = runs
    assert destroy["tf_vars"] == {
        "region": "us-east1",
        "observed_deployment_name": "it-support-agent",
        "project_id": "p2",
    }
    assert destroy["destroy"] and destroy["apply"]


def test_destroy_without_apply_only_plans(wrappers, monkeypatch, tmp_path):
    runs = _destroy_recorder(monkeypatch, tmp_path)
    acli_infra.main(["--destroy"])
    assert runs[0]["destroy"] and not runs[0]["apply"]


def test_destroy_forgets_the_engine_it_destroyed(
    wrappers, monkeypatch, tmp_path
):
    """`agents-cli aqua` would otherwise keep addressing a deleted engine."""
    _destroy_recorder(monkeypatch, tmp_path)
    metadata = tmp_path / _acli.STATE_DIR_NAME / _acli.METADATA_FILE
    metadata.write_text(
        json.dumps({"remote_agent_runtime_id": "x"}), encoding="utf-8"
    )

    acli_infra.main(["--destroy"])
    assert metadata.is_file(), "a plan destroys nothing, so it forgets nothing"

    acli_infra.main(["--destroy", "--apply"])
    assert not metadata.exists()


_INSTANCE = {
    "region": "us-east1",
    "aqua_engine": "projects/p2/locations/us-east1/reasoningEngines/1",
    "ambient_topic": "projects/p2/topics/it-support-agent-aqua-ambient",
    "ambient_caller_email": "caller@p2.iam.gserviceaccount.com",
}


def _write_attach_state(state_root: Path, agent: str, instance=_INSTANCE):
    """Writes an agent's attach state as `attach --apply` leaves it.

    Args:
        state_root: Directory holding state files.
        agent: Agent name.
        instance: The recorded `instance` output; None writes none.

    Returns:
        The state file's path.
    """
    state = state_root / f"attach-{agent}.tfstate"
    outputs = {} if instance is None else {"instance": {"value": instance}}
    state.write_text(json.dumps({"outputs": outputs}), encoding="utf-8")
    return state


def test_destroy_takes_the_attached_triggers_down_first(
    wrappers, monkeypatch, tmp_path
):
    """Left standing, they would call a deleted engine on every tick. What they
    call comes from their own state, not from the deployment's."""
    runs = _destroy_recorder(monkeypatch, tmp_path)
    state = _write_attach_state(
        tmp_path / _acli.STATE_DIR_NAME, "it_support_agent"
    )

    acli_infra.main(["--destroy", "--apply"])

    attach, deployment = runs
    assert attach["root"].parts[-2:] == ("examples", "attach")
    assert attach["state_key"] == "attach-it_support_agent"
    assert attach["tf_vars"] == {
        **_INSTANCE,
        "observed_agent_name": "it_support_agent",
    }
    assert attach["destroy"] and attach["apply"]
    assert deployment["state_key"] == "single-project"
    assert not state.exists(), (
        "a state left behind would wedge the next teardown"
    )


def test_destroy_leaves_triggers_that_call_another_aqua(
    wrappers, monkeypatch, tmp_path
):
    """A project can hold attach states for more than one AQuA."""
    runs = _destroy_recorder(monkeypatch, tmp_path)
    state_root = tmp_path / _acli.STATE_DIR_NAME
    (state_root / _acli.METADATA_FILE).write_text(
        json.dumps(
            {
                "remote_agent_runtime_id": "projects/p2/locations/x/reasoningEngines/9"
            }
        ),
        encoding="utf-8",
    )
    state = _write_attach_state(state_root, "it_support_agent")

    acli_infra.main(["--destroy", "--apply"])

    assert [run["state_key"] for run in runs] == ["single-project"]
    assert state.is_file()


def test_planning_the_teardown_keeps_the_attached_state(
    wrappers, monkeypatch, tmp_path
):
    """A plan destroys nothing, so the triggers are still there to destroy."""
    _destroy_recorder(monkeypatch, tmp_path)
    state = _write_attach_state(
        tmp_path / _acli.STATE_DIR_NAME, "it_support_agent"
    )

    acli_infra.main(["--destroy"])

    assert state.is_file()


def test_destroy_refuses_attached_triggers_it_cannot_name(
    wrappers, monkeypatch, tmp_path, capsys
):
    """Without the instance they call, the attach root cannot be planned."""
    runs = _destroy_recorder(monkeypatch, tmp_path)
    _write_attach_state(tmp_path / _acli.STATE_DIR_NAME, "a", instance=None)
    with pytest.raises(SystemExit):
        acli_infra.main(["--destroy", "--apply"])
    assert runs == [], "the instance must outlive the triggers that call it"
    assert "Delete them by hand" in capsys.readouterr().err


def test_destroy_is_a_no_op_without_state(wrappers, monkeypatch, tmp_path):
    """Nothing was provisioned, so there is nothing to tear down."""
    runs: list[Any] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda *a, **k: runs.append(a)
    )
    assert acli_infra.main(["--destroy", "--apply"]) == 0
    assert runs == []


@pytest.mark.parametrize(
    ("main", "opt_in"),
    [
        (acli_infra.main, "--apply-aqua"),
        (acli_deploy.main, "--deploy-aqua"),
    ],
)
def test_skip_aqua_is_refused_with_the_flag_that_opts_in(
    wrappers, capsys, main, opt_in
):
    """The built-in would reject it without saying what AQuA's step needs."""
    with pytest.raises(SystemExit):
        main(["--skip-aqua"])
    assert wrappers.calls == []
    assert opt_in in capsys.readouterr().err


def test_destroy_takes_the_opt_in_without_complaint(
    wrappers, monkeypatch, tmp_path
):
    """`--destroy` takes only AQuA down, so the flag changes nothing."""
    _destroy_recorder(monkeypatch, tmp_path)
    assert acli_infra.main(["--destroy", "--apply-aqua"]) == 0


def test_run_terraform_destroys_only_when_asked_to_apply(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(terraform_state.subprocess, "run", recorder)
    monkeypatch.setattr(
        terraform_state.shutil, "which", lambda tool: f"/usr/bin/{tool}"
    )
    for apply, expected in (
        (False, ["terraform", "plan", "-destroy"]),
        (True, ["terraform", "destroy", "-auto-approve"]),
    ):
        recorder.calls.clear()
        _acli.run_terraform(
            tmp_path / "root",
            state_key="single-project",
            state_root=tmp_path / ".aqua",
            apply=apply,
            destroy=True,
            tf_vars={},
        )
        _, action = (call["argv"] for call in recorder.calls)
        assert action == [*expected, "-input=false"]


def test_run_terraform_relocates_state_out_of_the_replaceable_tree(
    tmp_path, monkeypatch
):
    recorder = Recorder()
    monkeypatch.setattr(terraform_state.subprocess, "run", recorder)
    # Terraform is absent from the unit-test runner, and this asserts on the
    # argv rather than on anything it would do.
    monkeypatch.setattr(
        terraform_state.shutil, "which", lambda tool: f"/usr/bin/{tool}"
    )
    _acli.run_terraform(
        tmp_path / "root",
        state_key="single-project",
        state_root=tmp_path / ".aqua",
        apply=True,
        tf_vars={"region": "us-east1"},
    )
    init, apply = (call["argv"] for call in recorder.calls)
    assert init[:3] == ["terraform", "init", "-input=false"]
    assert (
        init[3]
        == f"-backend-config=path={tmp_path / '.aqua/single-project.tfstate'}"
    )
    assert apply[:4] == ["terraform", "apply", "-auto-approve", "-input=false"]
    assert apply[4:] == ["-var", "region=us-east1"]
    data_dir = recorder.calls[0]["env"]["TF_DATA_DIR"]
    assert data_dir == str(tmp_path / ".aqua" / ".terraform" / "single-project")
    assert Path(data_dir).is_dir()


def test_run_terraform_says_which_tool_is_missing(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(terraform_state.subprocess, "run", recorder)
    monkeypatch.setattr(terraform_state.shutil, "which", lambda tool: None)
    with pytest.raises(SystemExit):
        _acli.run_terraform(
            tmp_path / "root",
            state_key="bootstrap",
            state_root=tmp_path / ".aqua",
            apply=False,
            tf_vars={},
        )
    assert recorder.calls == [], "nothing should run without terraform"


# --- deploy -----------------------------------------------------------------


def _deploy_calls(recorder: Recorder) -> list[list[str]]:
    """Filter recorded commands to deploy operations.

    Excludes source snapshot publishing and `agents-cli setup` subprocesses
    so assertions can verify deployment invocations directly.

    Args:
        recorder: Test recorder capturing `_acli.run` invocations.

    Returns:
        Command-line argument lists for deployment operations.
    """
    return [
        call["argv"]
        for call in recorder.calls
        if "publish-source" not in call["argv"]
        and not _is_skill_install(call["argv"])
    ]


def test_deploy_runs_the_builtin_then_aqua(wrappers):
    acli_deploy.main(["--project", "p2", "--deploy-aqua"])
    assert wrappers.calls[0]["argv"][1:] == ["deploy", "--project", "p2"]
    assert wrappers.argv_containing("--update-only") == [
        "/bin/agents-cli",
        "deploy",
        "--update-only",
        "--service-name",
        "it-support-agent-aqua",
        "--project",
        "p2",
        "--region",
        "us-east1",
        "--update-env-vars",
        "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,"
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY",
    ]


def test_deploy_asks_for_content_capture_on_aqua_only(wrappers):
    # Ensures content capture variables are injected only into AQuA's deployment,
    # not the observed agent.
    acli_deploy.main(["--project", "p2", "--deploy-aqua"])
    assert "--update-env-vars" not in wrappers.calls[0]["argv"]


def test_an_operator_supplied_env_var_beats_aquas_default(wrappers):
    acli_deploy.main(
        [
            "--update-env-vars",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT,FOO=bar",
            "--deploy-aqua",
        ]
    )
    argv = wrappers.argv_containing("--update-only")
    pairs = _env_pairs(argv[argv.index("--update-env-vars") + 1])
    assert (
        pairs["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]
        == "NO_CONTENT"
    )
    assert (
        pairs["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    )
    assert pairs["FOO"] == "bar"


@pytest.mark.parametrize(
    "argv", [[], ["--update-env-vars", ""], ["--update-env-vars=FOO=bar"]]
)
def test_merging_env_vars_always_keeps_the_capture_defaults(argv):
    pairs = _env_pairs(acli_deploy.merge_update_env_vars(argv))
    assert (
        pairs["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"]
        == "SPAN_ONLY"
    )
    assert (
        pairs["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    )


def _env_pairs(value: str) -> dict[str, str]:
    """Parses comma-separated `KEY=VALUE` pairs into a dictionary.

    Args:
        value: Comma-separated `KEY=VALUE` string.

    Returns:
        Mapping of environment variable keys to their string values.
    """
    return dict(pair.split("=", 1) for pair in value.split(","))


def test_deploy_builds_aqua_from_the_vendored_tree(wrappers):
    acli_deploy.main(["--deploy-aqua"])
    aqua_call = next(c for c in wrappers.calls if "--update-only" in c["argv"])
    assert aqua_call["cwd"] == str(REPO_ROOT)


def test_deploy_honours_region_and_no_wait_from_argv(wrappers):
    acli_deploy.main(["--region", "europe-west1", "--no-wait", "--deploy-aqua"])
    argv = wrappers.argv_containing("--update-only")
    assert argv[argv.index("--region") + 1] == "europe-west1"
    assert "--no-wait" in argv


def test_deploy_leaves_aqua_out_without_its_flag(wrappers, capsys):
    acli_deploy.main([])
    assert wrappers.calls[0]["argv"][1:] == ["deploy"]
    assert len(_deploy_calls(wrappers)) == 1
    assert "--deploy-aqua" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--dry-run", "--status", "--list"])
def test_deploy_leaves_aqua_alone_for_read_only_runs(wrappers, flag):
    """`deploy --status` reports; it must not deploy the observer."""
    acli_deploy.main([flag])
    assert len(wrappers.calls) == 1


def test_deploy_copies_the_metadata_out_of_the_replaceable_tree(
    wrappers, tmp_path, monkeypatch
):
    sources = tmp_path / "extensions" / "aqua"
    sources.mkdir(parents=True)
    (sources / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (sources / _acli.METADATA_FILE).write_text(
        json.dumps({"remote_agent_runtime_id": "projects/p/…/9"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(acli_deploy, "resolve_extension_root", lambda: sources)
    acli_deploy.main(["--deploy-aqua", "--skip-aqua-ui"])
    copied = tmp_path / _acli.STATE_DIR_NAME / _acli.METADATA_FILE
    assert json.loads(copied.read_text(encoding="utf-8")) == {
        "remote_agent_runtime_id": "projects/p/…/9"
    }


def test_deploy_says_what_creates_an_engine_it_could_not_update(
    wrappers, monkeypatch, capsys
):
    """`--update-only` fails on an absent engine, naming it but not its maker."""
    monkeypatch.setattr(
        acli_deploy, "run", Recorder({"--update-only": (1, "")})
    )
    with pytest.raises(SystemExit) as excinfo:
        acli_deploy.main(["--deploy-aqua"])
    assert excinfo.value.code == 1
    assert (
        "agents-cli infra single-project --apply --apply-aqua"
        in capsys.readouterr().err
    )


def test_deploy_stops_when_the_agent_leg_fails(monkeypatch, tmp_path):
    # Chdir to tmp_path so ignore checks do not touch the repo's `.gitignore`.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        acli_deploy,
        "run_builtin",
        lambda *a: (_ for _ in ()).throw(SystemExit(3)),
    )
    with pytest.raises(SystemExit) as excinfo:
        acli_deploy.main([])
    assert excinfo.value.code == 3


# --- deploy: the dashboard step ----------------------------------------------


def test_deploy_ends_with_the_dashboard(wrappers):
    """Dashboard deploys last from the staged build context."""
    acli_deploy.main(["--project", "p2", "--deploy-aqua"])
    assert wrappers.calls[-1]["argv"] == [
        "/bin/agents-cli",
        "deploy",
        "--service-name",
        UI_SERVICE,
        "--project",
        "p2",
        "--region",
        "us-east1",
    ]
    assert wrappers.calls[-1]["cwd"] == str(STAGED_CONTEXT)


def test_deploy_asks_cloud_run_before_deploying_the_dashboard(wrappers):
    """Existence is checked before deploy because Cloud Run rejects `--update-only`."""
    acli_deploy.main(["--project", "p2", "--deploy-aqua"])
    assert wrappers.argv_containing("list") == [
        "/bin/gcloud",
        "run",
        "services",
        "list",
        "--project=p2",
        "--region=us-east1",
        f"--filter=metadata.name={UI_SERVICE}",
        "--format=value(metadata.name)",
    ]


def test_deploy_skips_the_dashboard_when_the_service_is_absent(
    wrappers, monkeypatch, capsys
):
    """Missing services are skipped to avoid misconfigured creation."""
    monkeypatch.setattr(
        acli_ui, "find_ui_service_for_deployment", lambda name, **kwargs: None
    )
    assert acli_deploy.main(["--deploy-aqua"]) == 0
    assert len(_deploy_calls(wrappers)) == 2, (
        "the built-in and the engine, and no more"
    )
    assert "nothing to deploy onto" in capsys.readouterr().err


def test_find_service_returns_the_name_cloud_run_reports(wrappers):
    """Return the service name when found."""
    found = acli_ui.find_ui_service_for_deployment(
        UI_SERVICE, project="p2", region="us-east1"
    )
    assert found == UI_SERVICE


def test_find_service_returns_none_when_cloud_run_lists_nothing(monkeypatch):
    monkeypatch.setattr(acli_ui, "run", Recorder({"list": (0, "\n")}))
    monkeypatch.setattr(acli_ui, "resolve_gcloud_path", lambda: "/bin/gcloud")
    assert (
        acli_ui.find_ui_service_for_deployment(
            "absent", project="p2", region="us-east1"
        )
        is None
    )


def test_deploy_fails_when_cloud_run_cannot_be_asked(wrappers, monkeypatch):
    """Fail if probing Cloud Run errors, rather than misinterpreting it as absent."""
    monkeypatch.setattr(acli_ui, "run", Recorder({"list": (1, "")}))
    with pytest.raises(SystemExit) as excinfo:
        acli_deploy.main(["--deploy-aqua"])
    assert excinfo.value.code == 1


def test_deploy_skips_the_dashboard_on_request(wrappers):
    acli_deploy.main(["--deploy-aqua", "--skip-aqua-ui"])
    assert len(_deploy_calls(wrappers)) == 2
    assert wrappers.calls[0]["argv"][1:] == ["deploy"], (
        "the built-in rejects the flag"
    )


def test_deploy_keeps_the_agents_image_away_from_the_dashboard(wrappers):
    """`--image` for the agent is not passed to the dashboard deploy."""
    acli_deploy.main(
        [
            "--project",
            "p2",
            "--image",
            "us-docker.pkg.dev/p/agent:1",
            "--deploy-aqua",
        ]
    )
    dashboard = wrappers.calls[-1]["argv"]
    assert UI_SERVICE in dashboard and "--image" not in dashboard


def test_deploy_honours_region_and_no_wait_for_the_dashboard(wrappers):
    acli_deploy.main(["--region", "europe-west1", "--no-wait", "--deploy-aqua"])
    dashboard = wrappers.calls[-1]["argv"]
    assert dashboard[dashboard.index("--region") + 1] == "europe-west1"
    assert "--no-wait" in dashboard
    assert "--region=europe-west1" in wrappers.argv_containing("list")


# --- deploy: staging the dashboard's build context ---------------------------


def _ui_sources(root: Path) -> Path:
    """Creates stub source files in the two directories staged for deploy.

    Args:
        root: Root path where staged source trees are created.

    Returns:
        The provided root path.
    """
    for tree, name in (
        (acli_ui.PROJECT_TREE, "Dockerfile"),
        (acli_ui.SHARED_TREE, "protocol.py"),
    ):
        (root / tree).mkdir(parents=True, exist_ok=True)
        (root / tree / name).write_text("", encoding="utf-8")
    return root


def test_staging_gathers_both_trees(tmp_path):
    """Staging collects files from both source directories."""
    context = acli_ui.build_ui_staging_dir_for_deployment(REPO_ROOT, tmp_path)
    assert context == tmp_path / _acli.UI_BUILD_DIR_NAME
    assert (context / "agents-cli-manifest.yaml").is_file()
    assert (context / "Dockerfile").is_file()
    assert (context / "app.py").is_file()
    assert (context / "ambient_quality_shared" / "agent_client.py").is_file()


def test_staging_leaves_out_what_the_image_builds_or_never_needs(tmp_path):
    root = _ui_sources(tmp_path / "sources")
    (root / acli_ui.PROJECT_TREE / "web" / "node_modules").mkdir(parents=True)
    (root / acli_ui.PROJECT_TREE / "static_v2").mkdir()
    (root / acli_ui.PROJECT_TREE / "__pycache__").mkdir()

    context = acli_ui.build_ui_staging_dir_for_deployment(root, tmp_path)
    assert not (context / "web" / "node_modules").exists()
    assert not (context / "static_v2").exists()
    assert not (context / "__pycache__").exists()


def test_staging_starts_from_an_empty_directory(tmp_path):
    """Staging clears existing files so deleted sources do not persist."""
    root = _ui_sources(tmp_path / "sources")
    stale = (
        acli_ui.build_ui_staging_dir_for_deployment(root, tmp_path)
        / "deleted-upstream.py"
    )
    stale.write_text("", encoding="utf-8")

    acli_ui.build_ui_staging_dir_for_deployment(root, tmp_path)
    assert not stale.exists()


def test_staging_says_which_tree_is_missing(tmp_path):
    """Errors identify which required source directory is missing."""
    with pytest.raises(SystemExit):
        acli_ui.build_ui_staging_dir_for_deployment(
            tmp_path / "empty", tmp_path / "state"
        )


# --- AQuA's own checkout ------------------------------------------------------

# The agent a standalone run is told to watch. Deliberately not the project
# `agents-cli info` answers with, which is AQuA itself in a real checkout.
OBSERVED = ["--observed-deployment-name", "sherlock-v2"]
OBSERVED_AGENT = ["--observed-agent-name", "sherlock_v2_agent"]
OBSERVED_ARGS = [*OBSERVED, *OBSERVED_AGENT]


def _unreachable(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"should not have been called: {args} {kwargs}")


def test_the_extension_recognises_its_own_checkout(own_checkout):
    assert _acli.is_aqua_own_checkout()


def test_the_vendored_extension_is_not_the_project(wrappers):
    """`extensions/aqua/` under a project root: two trees, not one."""
    assert not _acli.is_aqua_own_checkout()


def test_infra_in_its_own_checkout_has_no_builtin_to_wrap(
    own_checkout, monkeypatch
):
    """The observed agent's infrastructure belongs to the observed project."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    assert acli_infra.main(["--apply", *OBSERVED_ARGS]) == 0
    assert own_checkout.calls == []
    assert [run["state_key"] for run in runs] == ["bootstrap", "single-project"]


def test_infra_in_its_own_checkout_names_the_agent_it_was_told_to_watch(
    own_checkout, monkeypatch
):
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    acli_infra.main(["--apply", "--project", "p2", *OBSERVED_ARGS])
    # No `telemetry_table`: only the observed project knows its deployment
    # target, so the root's default stands.
    assert runs[1]["tf_vars"] == {
        "region": "us-east1",
        "observed_deployment_name": "sherlock-v2",
        "observed_agent_name": "sherlock_v2_agent",
        "project_id": "p2",
    }


def test_infra_in_its_own_checkout_asks_no_other_project_for_an_engine(
    own_checkout, monkeypatch
):
    """`infra show` reads a Terraform root that is in the observed project."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", _unreachable
    )
    acli_infra.main(["--apply", *OBSERVED_ARGS])
    assert "observed_engine_resource_name" not in runs[1]["tf_vars"]


def test_infra_in_its_own_checkout_refuses_to_guess_the_observed_deployment(
    own_checkout, monkeypatch, capsys
):
    """An agent named without its deployment has nothing to name AQuA after."""
    monkeypatch.setattr(acli_infra, "run_terraform", _unreachable)
    with pytest.raises(SystemExit):
        acli_infra.main(["--apply", *OBSERVED_AGENT])
    assert "--observed-deployment-name" in capsys.readouterr().err


def test_infra_in_its_own_checkout_names_an_unobserving_aqua_by_default(
    own_checkout, monkeypatch, capsys
):
    """So `infra`, `deploy` and `--destroy` agree without the flag on each."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    assert acli_infra.main(["--apply"]) == 0
    assert runs[1]["tf_vars"]["observed_deployment_name"] == "aqua-solo"
    assert runs[1]["tf_vars"]["configure_observed_agent"] == "false"
    assert "'aqua-solo'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--observed-deployment-name"],
        ["--observed-deployment-name", "--apply"],
        ["--observed-agent-name"],
    ],
)
def test_an_observed_option_without_a_value_fails(
    own_checkout, monkeypatch, argv
):
    """Dropped quietly, it would fall through to the default name."""
    monkeypatch.setattr(acli_infra, "run_terraform", _unreachable)
    with pytest.raises(SystemExit):
        acli_infra.main(argv)


def test_infra_in_its_own_checkout_can_provision_aqua_with_no_observed_agent(
    own_checkout, monkeypatch, capsys
):
    """One is attached later, and the deployment still needs a name."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", _unreachable
    )

    assert acli_infra.main(["--apply", "--project", "p2", *OBSERVED]) == 0

    assert runs[1]["tf_vars"] == {
        "region": "us-east1",
        "observed_deployment_name": "sherlock-v2",
        "configure_observed_agent": "false",
        "project_id": "p2",
    }
    err = capsys.readouterr().err
    assert "agents-cli aqua attach --apply --observed-agent-resource" in err
    assert "TF_VAR_observed_engine_id" not in err


def test_the_vendored_install_always_names_the_observed_agent(
    wrappers, monkeypatch
):
    """The project it runs in is the observed agent, so there is one to name."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", lambda *a, **k: None
    )
    acli_infra.main(["--apply", "--apply-aqua"])
    assert "configure_observed_agent" not in runs[1]["tf_vars"]
    assert runs[1]["tf_vars"]["observed_agent_name"] == "it_support_agent"


def test_infra_in_its_own_checkout_refuses_to_skip_aqua(
    own_checkout, monkeypatch
):
    monkeypatch.setattr(acli_infra, "run_terraform", _unreachable)
    with pytest.raises(SystemExit):
        acli_infra.main(["--apply", "--skip-aqua", *OBSERVED_ARGS])


def test_destroy_in_its_own_checkout_needs_only_the_deployment(
    own_checkout, monkeypatch, tmp_path
):
    """A teardown builds nothing, so it does not ask what ADK calls the agent."""
    runs = _destroy_recorder(monkeypatch, tmp_path)
    assert acli_infra.main(["--destroy", "--apply", *OBSERVED]) == 0
    assert runs[0]["tf_vars"]["observed_deployment_name"] == "sherlock-v2"


def test_the_observed_options_never_reach_the_builtin(wrappers, monkeypatch):
    """They are the extension's own, and Click rejects what it does not declare."""
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        acli_infra, "run_terraform", lambda root, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", lambda *a, **k: None
    )
    acli_infra.main(
        ["--apply", "--apply-aqua", "--observed-agent-name", "renamed_agent"]
    )
    assert wrappers.calls[0]["argv"][1:] == [
        "infra",
        "single-project",
        "--apply",
    ]
    assert runs[1]["tf_vars"]["observed_agent_name"] == "renamed_agent"


def test_deploy_in_its_own_checkout_is_aqua_and_its_dashboard(
    own_checkout, tmp_path
):
    assert acli_deploy.main(["--project", "p2", *OBSERVED_ARGS]) == 0
    engine, _, dashboard = own_checkout.calls
    assert engine["argv"][:5] == [
        "/bin/agents-cli",
        "deploy",
        "--update-only",
        "--service-name",
        "sherlock-v2-aqua",
    ]
    assert engine["cwd"] == str(tmp_path.resolve())
    assert dashboard["argv"][:4] == [
        "/bin/agents-cli",
        "deploy",
        "--service-name",
        "sherlock-v2-aqua-ui",
    ]


def test_deploy_in_its_own_checkout_needs_only_the_deployment(own_checkout):
    """What `infra single-project` provisioned with no observed agent deploys too."""
    assert acli_deploy.main(["--project", "p2", *OBSERVED]) == 0
    engine, _, dashboard = own_checkout.calls
    assert "sherlock-v2-aqua" in engine["argv"]
    assert "sherlock-v2-aqua-ui" in dashboard["argv"]


def test_deploy_in_its_own_checkout_publishes_no_source_snapshot(own_checkout):
    """The observed agent's code is in its own tree, and is snapshotted there."""
    acli_deploy.main(OBSERVED_ARGS)
    assert _deploy_calls(own_checkout) == [
        call["argv"] for call in own_checkout.calls
    ]


def test_deploy_in_its_own_checkout_refuses_to_guess_the_observed_deployment(
    own_checkout, capsys
):
    with pytest.raises(SystemExit):
        acli_deploy.main(OBSERVED_AGENT)
    assert own_checkout.calls == []
    assert "--observed-deployment-name" in capsys.readouterr().err


def test_deploy_in_its_own_checkout_uses_the_default_name(own_checkout):
    """The name `infra single-project` gave an AQuA with no observed agent."""
    assert acli_deploy.main([]) == 0
    engine, _, dashboard = own_checkout.calls
    assert "aqua-solo-aqua" in engine["argv"]
    assert "aqua-solo-aqua-ui" in dashboard["argv"]


def test_deploy_in_its_own_checkout_refuses_a_service_name(
    own_checkout, capsys
):
    """It would name an engine Terraform did not create, and the dashboard's own."""
    with pytest.raises(SystemExit):
        acli_deploy.main(["--service-name", "sherlock-v2-aqua", *OBSERVED_ARGS])
    assert own_checkout.calls == []
    message = capsys.readouterr().err
    # The flag names where the deployment comes from; it is not itself part of
    # the engine name, which is composed from that flag's value.
    assert "--observed-deployment-name" in message
    assert "<deployment>-aqua" in message
    assert "--observed-deployment-name-aqua" not in message


def test_deploy_in_its_own_checkout_refuses_to_skip_aqua(own_checkout):
    with pytest.raises(SystemExit):
        acli_deploy.main(["--skip-aqua", *OBSERVED_ARGS])
    assert own_checkout.calls == []


def test_deploy_in_its_own_checkout_takes_the_opt_in_without_complaint(
    own_checkout,
):
    """AQuA is all there is to deploy here, so the flag changes nothing."""
    assert acli_deploy.main(["--deploy-aqua", *OBSERVED_ARGS]) == 0
    assert "--deploy-aqua" not in own_checkout.calls[0]["argv"]
    assert len(own_checkout.calls) == 3


@pytest.mark.parametrize("flag", ["--dry-run", "--status", "--list"])
def test_deploy_in_its_own_checkout_reports_on_aqua_itself(own_checkout, flag):
    """AQuA is the deployment here, so a report has to be a report of it."""
    assert acli_deploy.main([flag, *OBSERVED_ARGS]) == 0
    (reported,) = own_checkout.calls
    assert reported["argv"][-1] == flag
    assert "sherlock-v2-aqua" in reported["argv"]


# --- keeping `.aqua/` out of git and out of the agent's upload ---------------


def test_state_dir_is_ignored_even_when_the_project_has_no_gitignore(tmp_path):
    """`.gitignore` is created to exclude `.aqua/` if missing."""
    _acli.ensure_state_dir_ignored(tmp_path)
    assert (
        IGNORE_ENTRY
        in (tmp_path / ".gitignore").read_text(encoding="utf-8").split()
    )


def test_ignoring_the_state_dir_preserves_what_is_already_there(tmp_path):
    (tmp_path / ".gitignore").write_text(
        ".venv\n__pycache__/\n", encoding="utf-8"
    )
    _acli.ensure_state_dir_ignored(tmp_path)
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").split()
    assert lines[:2] == [".venv", "__pycache__/"]
    assert IGNORE_ENTRY in lines


def test_ignoring_the_state_dir_twice_writes_one_entry(tmp_path):
    """Repeated ignore checks do not duplicate the entry."""
    for _ in range(3):
        _acli.ensure_state_dir_ignored(tmp_path)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.count(IGNORE_ENTRY) == 1


def test_a_gcloudignore_that_reads_gitignore_needs_no_entry_of_its_own(
    tmp_path,
):
    """`.gcloudignore` needs no entry when it includes `.gitignore`."""
    gcloudignore = tmp_path / ".gcloudignore"
    gcloudignore.write_text(
        "#!include:.gitignore\n/extensions/\n", encoding="utf-8"
    )
    _acli.ensure_state_dir_ignored(tmp_path)
    assert IGNORE_ENTRY not in gcloudignore.read_text(encoding="utf-8")


def test_a_gcloudignore_that_stands_alone_gets_the_entry_too(tmp_path):
    """Standalone `.gcloudignore` files receive their own ignore entry."""
    gcloudignore = tmp_path / ".gcloudignore"
    gcloudignore.write_text(".git\n/extensions/\n", encoding="utf-8")
    _acli.ensure_state_dir_ignored(tmp_path)
    assert IGNORE_ENTRY in gcloudignore.read_text(encoding="utf-8").split()


def test_state_dir_asserts_the_entry_when_it_creates_the_directory(tmp_path):
    """Creating the state directory also adds the ignore entry."""
    created = _acli.ensure_aqua_state_dir({"project_root": str(tmp_path)})
    assert created.is_dir()
    assert (
        IGNORE_ENTRY
        in (tmp_path / ".gitignore").read_text(encoding="utf-8").split()
    )


def test_deploy_ignores_the_state_dir_before_the_agent_is_packaged(
    wrappers, tmp_path
):
    """The ignore entry is added before the built-in packages files."""
    seen: list[bool] = []
    gitignore = tmp_path / ".gitignore"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        acli_deploy,
        "run_builtin",
        lambda *a: seen.append(
            gitignore.is_file()
            and IGNORE_ENTRY in gitignore.read_text().split()
        ),
    )
    try:
        acli_deploy.main([])
    finally:
        monkeypatch.undo()
    assert seen == [True]


def test_deploy_outside_a_project_leaves_ignore_files_alone(
    wrappers, monkeypatch, tmp_path_factory
):
    """Deployments outside a project root do not create ignore files."""
    elsewhere = tmp_path_factory.mktemp("no-project")
    monkeypatch.chdir(elsewhere)
    acli_deploy.main(["--deployment-target", "cloud_run"])
    assert not (elsewhere / ".gitignore").exists()


# --- the aqua command -------------------------------------------------------


def test_recorded_engine_id_is_read_from_the_state_directory(
    tmp_path, monkeypatch
):
    state = tmp_path / _acli.STATE_DIR_NAME
    state.mkdir()
    (state / _acli.METADATA_FILE).write_text(
        json.dumps({"remote_agent_runtime_id": "projects/p/…/9"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert acli_aqua.read_recorded_engine_id() == "projects/p/…/9"


def test_recorded_engine_id_tolerates_a_missing_or_broken_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert acli_aqua.read_recorded_engine_id() is None
    state = tmp_path / _acli.STATE_DIR_NAME
    state.mkdir()
    (state / _acli.METADATA_FILE).write_text("not json", encoding="utf-8")
    assert acli_aqua.read_recorded_engine_id() is None
    # Truncated mid-write, so not even text: still a report, not a traceback.
    (state / _acli.METADATA_FILE).write_bytes(b"\xff\xfe{")
    assert acli_aqua.read_recorded_engine_id() is None


def _record_deployment(
    tmp_path, engine="projects/p/locations/l/reasoningEngines/9"
):
    state = tmp_path / _acli.STATE_DIR_NAME
    state.mkdir(exist_ok=True)
    (state / _acli.METADATA_FILE).write_text(
        json.dumps({"remote_agent_runtime_id": engine, "region": "us-east1"}),
        encoding="utf-8",
    )
    return engine


def _provision_ui(
    tmp_path, name="proj-aqua-ui", uri="https://proj-aqua-ui.run.app"
):
    """Writes deployment root state with outputs recorded by `--apply`.

    Args:
        tmp_path: Temporary directory path.
        name: UI service name.
        uri: UI service URI.

    Returns:
        A tuple of (name, uri).
    """
    state = tmp_path / _acli.STATE_DIR_NAME
    state.mkdir(exist_ok=True)
    _acli.build_state_path(state, _acli.DEPLOYMENT_KEY).write_text(
        json.dumps(
            {
                "version": 4,
                "outputs": {
                    "ui_service_name": {"value": name, "type": "string"},
                    "ui_service_uri": {"value": uri, "type": "string"},
                },
            }
        ),
        encoding="utf-8",
    )
    return name, uri


def test_info_prints_the_deployment_record(tmp_path, monkeypatch, capsys):
    """Output a single JSON document so documentation jq pipelines work."""
    engine = _record_deployment(tmp_path)
    name, uri = _provision_ui(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info"]) == 0

    reported = json.loads(capsys.readouterr().out)
    assert reported["remote_agent_runtime_id"] == engine
    assert reported["region"] == "us-east1"
    assert reported["ui"] == {"service_name": name, "url": uri}


def test_info_reports_a_dashboard_that_does_not_exist_as_null(
    tmp_path, monkeypatch, capsys
):
    """An unapplied Terraform root is a valid state to report, not a failure."""
    _record_deployment(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info"]) == 0

    assert json.loads(capsys.readouterr().out)["ui"] == {
        "service_name": None,
        "url": None,
    }


def test_info_ui_flags_print_terraform_s_outputs_alone(
    tmp_path, monkeypatch, capsys
):
    """Print individual bare values to stdout for shell capture."""
    name, uri = _provision_ui(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--ui-service"]) == 0
    assert capsys.readouterr().out == f"{name}\n"

    assert acli_aqua.main(["info", "--ui-url"]) == 0
    assert capsys.readouterr().out == f"{uri}\n"


@pytest.mark.parametrize("flag", ["--ui-service", "--ui-url"])
def test_info_ui_flags_name_the_command_that_creates_the_dashboard(
    tmp_path, monkeypatch, capsys, flag
):
    _record_deployment(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", flag]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agents-cli infra single-project --apply" in captured.err


def test_info_rejects_two_flags_that_each_own_stdout(
    tmp_path, monkeypatch, capsys
):
    _provision_ui(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--ui-service", "--ui-url"]) == 2

    assert "only one may be given" in capsys.readouterr().err


def test_state_outputs_tolerates_a_missing_or_broken_state(tmp_path):
    assert _acli.read_state_outputs(tmp_path, _acli.DEPLOYMENT_KEY) == {}
    state = _acli.build_state_path(tmp_path, _acli.DEPLOYMENT_KEY)
    state.write_text("not json", encoding="utf-8")
    assert _acli.read_state_outputs(tmp_path, _acli.DEPLOYMENT_KEY) == {}
    # Truncated mid-write, so not even text: still a report, not a traceback.
    state.write_bytes(b"\xff\xfe{")
    assert _acli.read_state_outputs(tmp_path, _acli.DEPLOYMENT_KEY) == {}


def test_info_resource_prints_the_engine_id_alone(
    tmp_path, monkeypatch, capsys
):
    """One bare line, so a shell can capture it."""
    engine = _record_deployment(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--resource"]) == 0

    assert capsys.readouterr().out == f"{engine}\n"


def test_info_reports_the_environment_override(tmp_path, monkeypatch, capsys):
    """`info` answers what a command run now reaches, not what was deployed."""
    _record_deployment(tmp_path)
    monkeypatch.setenv("AGENT_ENGINE_RESOURCE_ID", "projects/other/…/1")
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--resource"]) == 0

    assert capsys.readouterr().out == "projects/other/…/1\n"


def test_info_keeps_the_override_out_of_the_json(tmp_path, monkeypatch, capsys):
    """The JSON is the project's record; the override is a property of this shell."""
    engine = _record_deployment(tmp_path)
    monkeypatch.setenv("AGENT_ENGINE_RESOURCE_ID", "projects/other/…/1")
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info"]) == 0

    reported = capsys.readouterr()
    assert json.loads(reported.out)["remote_agent_runtime_id"] == engine
    assert "projects/other/…/1" in reported.err


def test_info_fails_when_nothing_is_deployed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--resource"]) == 1

    assert "agents-cli deploy" in capsys.readouterr().err


def test_info_rejects_an_unknown_argument(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--engine"]) == 2

    assert "--engine" in capsys.readouterr().err


def test_info_region_prints_the_engines_location(tmp_path, monkeypatch, capsys):
    """What `gcloud run` wants, so it is a bare line and not the whole name."""
    _record_deployment(
        tmp_path, engine="projects/p/locations/us-east1/reasoningEngines/9"
    )
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--region"]) == 0

    assert capsys.readouterr().out == "us-east1\n"


def test_info_region_follows_the_environment_override(
    tmp_path, monkeypatch, capsys
):
    """The region has to name the engine `info` reports, not the recorded one."""
    _record_deployment(
        tmp_path, engine="projects/p/locations/us-east1/reasoningEngines/9"
    )
    monkeypatch.setenv(
        "AGENT_ENGINE_RESOURCE_ID",
        "projects/o/locations/europe-west1/reasoningEngines/1",
    )
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--region"]) == 0

    assert capsys.readouterr().out == "europe-west1\n"


def test_info_region_fails_when_nothing_is_deployed(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["info", "--region"]) == 1

    assert "agents-cli deploy" in capsys.readouterr().err


@pytest.mark.parametrize(
    "resource",
    ["", "not-a-resource", "projects/p/regions/us-east1/reasoningEngines/9"],
)
def test_engine_region_rejects_a_name_it_cannot_parse(resource):
    assert acli_aqua.extract_engine_location(resource) is None


def _capture_proxy(monkeypatch, returncode=0):
    """Records the gcloud command that `ui-proxy` builds.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        returncode: Return code to return from the fake process.

    Returns:
        A list of argv lists captured during execution.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(
        acli_aqua, "resolve_gcloud_path", lambda: "/usr/bin/gcloud"
    )
    monkeypatch.setattr(acli_aqua, "run", fake_run)
    return calls


def test_ui_proxy_fills_in_the_service_and_region(tmp_path, monkeypatch):
    """The two values that make the command awkward to type by hand."""
    _record_deployment(
        tmp_path, engine="projects/p/locations/us-east1/reasoningEngines/9"
    )
    name, _ = _provision_ui(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.chdir(tmp_path)
    calls = _capture_proxy(monkeypatch)

    assert acli_aqua.main(["ui-proxy"]) == 0

    assert calls == [
        [
            "/usr/bin/gcloud",
            "run",
            "services",
            "proxy",
            name,
            "--region=us-east1",
            "--project=proj",
        ]
    ]


def test_ui_proxy_lets_a_given_flag_win(tmp_path, monkeypatch):
    """A resolved default sent as well would reach gcloud as a duplicate."""
    _record_deployment(
        tmp_path, engine="projects/p/locations/us-east1/reasoningEngines/9"
    )
    _provision_ui(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.chdir(tmp_path)
    calls = _capture_proxy(monkeypatch)

    assert (
        acli_aqua.main(["ui-proxy", "--region", "europe-west1", "--port=9999"])
        == 0
    )

    assert "--region=us-east1" not in calls[0]
    assert calls[0][-3:] == ["--region", "europe-west1", "--port=9999"]


def test_ui_proxy_reports_a_missing_dashboard(tmp_path, monkeypatch, capsys):
    _record_deployment(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert acli_aqua.main(["ui-proxy"]) == 1

    assert "dashboard" in capsys.readouterr().err


def test_ui_proxy_hints_at_the_missing_component(tmp_path, monkeypatch, capsys):
    """gcloud offers to install it and then cannot, so say what actually works."""
    _record_deployment(
        tmp_path, engine="projects/p/locations/us-east1/reasoningEngines/9"
    )
    _provision_ui(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    _capture_proxy(monkeypatch, returncode=1)

    assert acli_aqua.main(["ui-proxy"]) == 1

    assert "google-cloud-cli-cloud-run-proxy" in capsys.readouterr().err


def test_info_never_reaches_the_cli(tmp_path, monkeypatch):
    """Handled in the wrapper, so it answers with the sources absent."""
    _record_deployment(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        acli_aqua,
        "resolve_extension_root",
        lambda: tmp_path / "no-such-extension",
    )

    assert acli_aqua.main(["info", "--resource"]) == 0


# --- aqua attach ------------------------------------------------------------


_ATTACH_INFO = {**_INFO, "root_agent_name": "it_support_root"}


def _record_observed_deployment(
    tmp_path, engine="projects/p/locations/l/reasoningEngines/42"
):
    """Writes the observed agent's own `deployment_metadata.json` at the project root.

    Args:
        tmp_path: Project root.
        engine: Engine resource name to record.
    """
    (tmp_path / _acli.METADATA_FILE).write_text(
        json.dumps({"remote_agent_runtime_id": engine}), encoding="utf-8"
    )


def _serve_infra_outputs(monkeypatch, outputs: dict[str, Any]) -> list[int]:
    """Answers `infra show` with ``outputs`` and counts how often it was asked.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        outputs: The `outputs` mapping to return.

    Returns:
        A list that gains one element per call.
    """
    calls: list[int] = []

    def fake() -> dict[str, Any]:
        calls.append(1)
        return outputs

    monkeypatch.setattr(acli_aqua, "load_infra_outputs", fake)
    return calls


def _fill(argv: list[str], tmp_path) -> tuple[list[str], list[str]]:
    info = dict(_ATTACH_INFO, project_root=str(tmp_path))
    discovered_options, notes = acli_aqua.resolve_discovered_options(argv, info)
    return acli_aqua.build_attach_argv(argv, discovered_options), notes


def test_attach_switches_match_the_cli():
    """The wrapper tells AGENT_NAME from an option's value by this set alone."""
    from ambient_quality_cli import aqua_cli

    switches = {"--help"}
    for param in aqua_cli.attach_agent_cmd.params:
        if getattr(param, "is_flag", False):
            switches.update(param.opts)
            switches.update(param.secondary_opts)
    assert switches == acli_aqua._ATTACH_SWITCHES


def test_target_flags_match_the_cli():
    """A target the user gives must win over the recorded deployment."""
    from ambient_quality_cli import aqua_cli

    targets = {
        opt
        for param in aqua_cli.attach_agent_cmd.params
        if param.name in ("resource", "url")
        for opt in param.opts
    }
    assert targets == set(acli_aqua._TARGET_FLAGS)


def _extract_hints(argv: list[str]) -> dict[str, Any]:
    """Decodes the `--hints` the wrapper added.

    Args:
        argv: Arguments the wrapper hands to the CLI.

    Returns:
        The hints' values by field; empty without `--hints`.
    """
    for arg in argv:
        if arg.startswith("--hints="):
            hints = json.loads(arg.removeprefix("--hints="))
            return {field: hint["value"] for field, hint in hints.items()}
    return {}


def test_attach_seeds_discovery_and_hints_the_rest(tmp_path, monkeypatch):
    _record_observed_deployment(tmp_path)
    _serve_infra_outputs(
        monkeypatch, {"telemetry_dataset_id": "agent_telemetry"}
    )

    argv, notes = _fill(["--dry-run"], tmp_path)

    assert argv[:2] == [
        "--dry-run",
        "--observed-agent-resource=projects/p/locations/l/reasoningEngines/42",
    ]
    assert _extract_hints(argv) == {
        "observed_agent_name": "it_support_root",
        "observed_deployment_name": "it-support-agent",
        "telemetry_ingestion_source": "cloud_logging",
        "telemetry_dataset": "agent_telemetry",
        "telemetry_table": "aiplatform_googleapis_com_reasoning_engine_stdout",
        "telemetry_location": "us-east1",
    }
    assert notes == []
    # What the CLI makes of it: every value lands on the parameter it names.
    from ambient_quality_cli import aqua_cli

    params = aqua_cli.attach_agent_cmd.make_context("attach", argv).params
    assert params["agent_name"] == ""
    assert params["observed_agent_resource"].endswith("/reasoningEngines/42")
    assert json.loads(params["hints"])["telemetry_location"]["source"] == (
        "region from agents-cli info"
    )


@pytest.mark.parametrize(
    "given",
    [
        [
            "--observed-agent-resource",
            "projects/p/locations/l/reasoningEngines/7",
        ],
        ["--telemetry-table=mine", "--deployment-name", "d"],
    ],
)
def test_attach_lets_a_given_flag_win(tmp_path, monkeypatch, given):
    _record_observed_deployment(tmp_path)
    _serve_infra_outputs(
        monkeypatch, {"telemetry_dataset_id": "agent_telemetry"}
    )

    argv, _ = _fill(["other_agent", *given], tmp_path)

    assert argv[: 1 + len(given)] == ["other_agent", *given]
    hints = _extract_hints(argv)
    assert "observed_agent_name" not in hints
    if "--observed-agent-resource" in given:
        assert sum(a.startswith("--observed-agent-resource") for a in argv) == 1
    else:
        assert "telemetry_table" not in hints
        assert "observed_deployment_name" not in hints


def test_attach_leaves_another_telemetry_source_to_its_flags(
    tmp_path, monkeypatch
):
    """The sink agents-cli creates says nothing about a BigQuery Analytics table."""
    calls = _serve_infra_outputs(
        monkeypatch, {"telemetry_dataset_id": "agent_telemetry"}
    )

    argv, _ = _fill(["--telemetry-source", "big_query"], tmp_path)

    assert not any(
        field.startswith("telemetry_") for field in _extract_hints(argv)
    )
    assert calls == [], "no reason to ask `infra show`"


def test_attach_says_what_provisions_a_missing_dataset(tmp_path, monkeypatch):
    """Without an engine, nothing else would say why telemetry is not filled."""
    _serve_infra_outputs(monkeypatch, {})

    argv, notes = _fill([], tmp_path)

    assert not any(
        field.startswith("telemetry_") for field in _extract_hints(argv)
    )
    assert any(
        "agents-cli infra single-project --apply" in note for note in notes
    )


def test_attach_leaves_a_missing_dataset_to_discovery(tmp_path, monkeypatch):
    """With an engine, discovery reports the sinks it does or does not find."""
    _record_observed_deployment(tmp_path)
    _serve_infra_outputs(monkeypatch, {})

    _, notes = _fill([], tmp_path)

    assert notes == []


def test_attach_hints_the_rest_of_the_sink_around_a_given_dataset(
    tmp_path, monkeypatch
):
    calls = _serve_infra_outputs(monkeypatch, {})

    argv, _ = _fill(["--telemetry-dataset", "mine"], tmp_path)

    hints = _extract_hints(argv)
    assert hints["telemetry_ingestion_source"] == "cloud_logging"
    assert hints["telemetry_location"] == "us-east1"
    assert "telemetry_dataset" not in hints
    assert calls == []


def test_attach_reads_the_observed_engine_not_aquas(tmp_path, monkeypatch):
    """`.aqua/deployment_metadata.json` is AQuA's own engine."""
    _record_deployment(tmp_path)
    _serve_infra_outputs(monkeypatch, {})

    argv, notes = _fill([], tmp_path)

    assert not any(arg.startswith("--observed-agent-resource") for arg in argv)
    assert any("nothing is discovered" in note for note in notes)


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        ([], False),
        (["name"], True),
        (["--dry-run", "name"], True),
        (["--lookback-days", "7"], False),
        (["--lookback-days=7", "name"], True),
        (["--no-default", "--yes"], False),
        (["--"], False),
        (["--", "name"], True),
    ],
)
def test_agent_name_argument_is_told_from_option_values(argv, named):
    assert acli_aqua.has_agent_name_argument(argv) is named


def test_discovered_options_go_before_a_double_dash():
    discovered_options = [
        acli_aqua.DiscoveredOption("observed_agent_name", "a", "s"),
        acli_aqua.DiscoveredOption("--observed-agent-resource", "r", "s"),
    ]
    argv = acli_aqua.build_attach_argv(["--yes", "--"], discovered_options)

    assert argv[:2] == ["--yes", "--observed-agent-resource=r"]
    assert argv[2].startswith("--hints=")
    assert argv[3:] == ["--"]


def _capture_cli(monkeypatch) -> list[list[str]]:
    """Replaces running AQuA's CLI with recording the argv it would get.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A list of the argv vectors handed to the CLI.
    """
    handed: list[list[str]] = []
    monkeypatch.setattr(
        acli_aqua.runpy, "run_path", lambda *a, **k: handed.append(sys.argv[1:])
    )
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    return handed


def test_attach_hands_the_filled_command_to_the_cli(
    wrappers, tmp_path, monkeypatch, capsys
):
    _record_observed_deployment(tmp_path)
    _serve_infra_outputs(
        monkeypatch, {"telemetry_dataset_id": "agent_telemetry"}
    )
    monkeypatch.setattr(
        acli_aqua,
        "load_project_info",
        lambda: dict(_ATTACH_INFO, project_root=str(tmp_path)),
    )
    handed = _capture_cli(monkeypatch)

    assert acli_aqua.main(["attach", "--dry-run"]) == 0

    assert handed[0][:2] == ["attach", "--dry-run"]
    assert (
        "--observed-agent-resource=projects/p/locations/l/reasoningEngines/42"
        in handed[0]
    )
    assert _extract_hints(handed[0])["observed_agent_name"] == "it_support_root"
    err = capsys.readouterr().err
    assert "./deployment_metadata.json" in err
    # The CLI shows the hints, beside what discovery finds.
    assert "root_agent_name" not in err


def test_attach_with_no_discover_is_left_as_given(
    wrappers, tmp_path, monkeypatch
):
    _record_observed_deployment(tmp_path)
    monkeypatch.setattr(acli_aqua, "load_project_info", _unreachable)
    handed = _capture_cli(monkeypatch)

    assert acli_aqua.main(["attach", "--no-discover"]) == 0

    # Only the source to publish is added: that is not discovery.
    assert handed == [["attach", "--no-discover", f"--source-root={tmp_path}"]]


@pytest.mark.parametrize("argv", [["attach", "--help"], ["list-agents"]])
def test_only_attach_is_filled_in(wrappers, monkeypatch, argv):
    monkeypatch.setattr(acli_aqua, "load_project_info", _unreachable)
    handed = _capture_cli(monkeypatch)

    assert acli_aqua.main(argv) == 0

    assert handed == [argv]


def test_attach_in_its_own_checkout_is_left_as_given(own_checkout, monkeypatch):
    """There the project is AQuA, which says nothing about the agent it watches."""
    monkeypatch.setattr(acli_aqua, "load_project_info", _unreachable)
    monkeypatch.setattr(acli_aqua, "resolve_extension_root", lambda: REPO_ROOT)
    handed = _capture_cli(monkeypatch)

    assert acli_aqua.main(["attach", "--dry-run"]) == 0

    assert handed == [["attach", "--dry-run"]]


_AQUA_ELSEWHERE = "projects/p/locations/us-central1/reasoningEngines/77"


def _exit_cli(monkeypatch, code: int) -> list[str | None]:
    """Replaces running AQuA's CLI with exiting the way Click does.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        code: Exit code the CLI ends with.

    Returns:
        The `AGENT_ENGINE_RESOURCE_ID` each run saw.
    """
    targets: list[str | None] = []

    def run_path(*args: Any, **kwargs: Any) -> None:
        targets.append(os.environ.get("AGENT_ENGINE_RESOURCE_ID"))
        raise SystemExit(code)

    monkeypatch.setattr(acli_aqua.runpy, "run_path", run_path)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    return targets


def _attach_from_the_observed_project(monkeypatch, tmp_path, *argv: str) -> int:
    """Runs `agents-cli aqua attach` in the observed agent's project.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: The project root.
        *argv: Arguments after `attach`.

    Returns:
        The exit code the CLI ended with.
    """
    _record_observed_deployment(tmp_path)
    _serve_infra_outputs(monkeypatch, {})
    monkeypatch.setattr(
        acli_aqua,
        "load_project_info",
        lambda: dict(_ATTACH_INFO, project_root=str(tmp_path)),
    )
    with pytest.raises(SystemExit) as exit_:
        acli_aqua.main(["attach", *argv])
    return int(exit_.value.code or 0)


def test_attach_records_the_aqua_it_reached_for_later_commands(
    wrappers, tmp_path, monkeypatch, capsys
):
    """An AQuA deployed elsewhere is named once, on attach."""
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    targets = _exit_cli(monkeypatch, 0)

    code = _attach_from_the_observed_project(
        monkeypatch, tmp_path, "--apply", "--aqua-resource", _AQUA_ELSEWHERE
    )

    assert code == 0
    assert targets == [None], "the flag targets the attach itself"
    record = tmp_path / _acli.STATE_DIR_NAME / acli_aqua.ATTACHED_AQUA_FILE
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "aqua_resource": _AQUA_ELSEWHERE
    }
    assert "/.aqua/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "later `agents-cli aqua` commands here reach it" in (
        capsys.readouterr().err
    )

    with pytest.raises(SystemExit):
        acli_aqua.main(["list-agents"])
    assert targets[-1] == _AQUA_ELSEWHERE
    assert acli_aqua.read_recorded_engine_id() == _AQUA_ELSEWHERE


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["--dry-run", "--aqua-resource", _AQUA_ELSEWHERE], 0),
        ([f"--aqua-resource={_AQUA_ELSEWHERE}"], 1),
        (["--url", "http://localhost:8000"], 0),
    ],
)
def test_attach_records_nothing_it_did_not_attach_to(
    wrappers, tmp_path, monkeypatch, argv, code
):
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    _exit_cli(monkeypatch, code)

    assert (
        _attach_from_the_observed_project(monkeypatch, tmp_path, *argv) == code
    )

    assert not (
        tmp_path / _acli.STATE_DIR_NAME / acli_aqua.ATTACHED_AQUA_FILE
    ).exists()


def test_attach_records_the_resource_spelling_too(
    wrappers, tmp_path, monkeypatch
):
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    _exit_cli(monkeypatch, 0)

    _attach_from_the_observed_project(
        monkeypatch, tmp_path, "--resource", _AQUA_ELSEWHERE
    )

    assert acli_aqua.read_attached_aqua_resource() == _AQUA_ELSEWHERE


def test_attach_leaves_the_aqua_a_project_deploys_in_charge(
    wrappers, tmp_path, monkeypatch, capsys
):
    """`.aqua/deployment_metadata.json` is the newer word on which AQuA is used."""
    deployed = _record_deployment(tmp_path)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    _exit_cli(monkeypatch, 0)

    _attach_from_the_observed_project(
        monkeypatch, tmp_path, "--aqua-resource", _AQUA_ELSEWHERE
    )

    assert acli_aqua.read_attached_aqua_resource() is None
    assert acli_aqua.read_recorded_engine_id() == deployed
    assert "deploys its own AQuA" in capsys.readouterr().err


def test_recorded_deployment_wins_over_an_attached_aqua(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / _acli.STATE_DIR_NAME
    state.mkdir()
    (state / acli_aqua.ATTACHED_AQUA_FILE).write_text(
        json.dumps({"aqua_resource": _AQUA_ELSEWHERE}), encoding="utf-8"
    )
    assert acli_aqua.read_recorded_engine_id() == _AQUA_ELSEWHERE

    deployed = _record_deployment(tmp_path)

    assert acli_aqua.read_recorded_engine_id() == deployed


def test_info_reports_an_attached_aqua(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    state = tmp_path / _acli.STATE_DIR_NAME
    state.mkdir()
    (state / acli_aqua.ATTACHED_AQUA_FILE).write_text(
        json.dumps({"aqua_resource": _AQUA_ELSEWHERE}), encoding="utf-8"
    )

    assert acli_aqua.main(["info"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["attached_aqua_resource"] == _AQUA_ELSEWHERE
    assert "No " not in captured.err

    assert acli_aqua.main(["info", "--region"]) == 0
    assert capsys.readouterr().out.strip() == "us-central1"


# --- the coding-agent skill ---------------------------------------------------


def _is_skill_install(argv: list[str]) -> bool:
    """Check whether a command invocation runs `agents-cli setup`.

    Args:
        argv: Recorded command-line argument list.

    Returns:
        True if the arguments run `agents-cli setup`, False otherwise.
    """
    return argv[1:2] == ["setup"]


def _skill_install_calls(recorder: Recorder) -> list[dict[str, Any]]:
    """Filter recorded calls to `agents-cli setup` skill installations.

    Args:
        recorder: Test recorder capturing `_acli.run` invocations.

    Returns:
        Recorded call dictionaries that execute `agents-cli setup`.
    """
    return [call for call in recorder.calls if _is_skill_install(call["argv"])]


def _expected_skill_install(project_root: Path) -> dict[str, Any]:
    """Build the expected call dictionary for installing the workspace skill.

    Args:
        project_root: Root directory of the observed agent project.

    Returns:
        Dictionary specifying expected argv, cwd, and env parameters.
    """
    return {
        "argv": [
            "/bin/agents-cli",
            "setup",
            "--workspace",
            "--skip-auth",
            "--skills-source",
            str(REPO_ROOT / "skills" / _acli.SKILL_NAME),
        ],
        "cwd": str(project_root),
        "env": None,
    }


def test_the_repository_ships_the_skill_it_installs():
    skill = REPO_ROOT / "skills" / _acli.SKILL_NAME / "SKILL.md"
    assert skill.is_file()
    _, frontmatter, _ = skill.read_text(encoding="utf-8").split("---", 2)
    assert yaml.safe_load(frontmatter)["name"] == _acli.SKILL_NAME


def _stub_infra_terraform(monkeypatch) -> None:
    """Stubs Terraform and engine lookup operations during infrastructure testing.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(acli_infra, "run_terraform", lambda *a, **k: None)
    monkeypatch.setattr(
        acli_infra, "read_observed_engine_resource_name", lambda *a, **k: None
    )


def test_infra_installs_the_skill_with_agents_cli_setup(
    wrappers, monkeypatch, tmp_path, capsys
):
    _stub_infra_terraform(monkeypatch)
    assert acli_infra.main(["--apply", "--apply-aqua"]) == 0
    assert _skill_install_calls(wrappers) == [_expected_skill_install(tmp_path)]
    assert "installed the agents-cli-aqua skill" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--apply", "--destroy"])
def test_infra_installs_no_skill_without_the_aqua_leg(
    wrappers, monkeypatch, flag
):
    _stub_infra_terraform(monkeypatch)
    assert acli_infra.main([flag]) == 0
    assert _skill_install_calls(wrappers) == []


def test_infra_in_its_own_checkout_installs_no_skill(own_checkout, monkeypatch):
    _stub_infra_terraform(monkeypatch)
    assert acli_infra.main(["--apply", *OBSERVED_ARGS]) == 0
    assert _skill_install_calls(own_checkout) == []


def test_deploy_installs_the_skill_with_agents_cli_setup(wrappers, tmp_path):
    assert acli_deploy.main(["--deploy-aqua"]) == 0
    assert _skill_install_calls(wrappers) == [_expected_skill_install(tmp_path)]


@pytest.mark.parametrize(
    "flag", ["--project=p2", *acli_deploy._READ_ONLY_FLAGS]
)
def test_deploy_installs_no_skill_without_the_aqua_leg(wrappers, flag):
    acli_deploy.main([flag])
    assert _skill_install_calls(wrappers) == []


def test_deploy_in_its_own_checkout_installs_no_skill(own_checkout):
    assert acli_deploy.main(OBSERVED_ARGS) == 0
    assert _skill_install_calls(own_checkout) == []


@pytest.fixture
def failing_setup(wrappers, monkeypatch):
    """Configure `_acli.run` to simulate a failed `agents-cli setup` with error output."""
    recorder = Recorder({"setup": (1, "npx: command not found\n")})
    monkeypatch.setattr(_acli, "run", recorder)
    return recorder


def test_a_failed_skill_install_does_not_fail_infra(
    failing_setup, monkeypatch, capsys
):
    _stub_infra_terraform(monkeypatch)
    assert acli_infra.main(["--apply", "--apply-aqua"]) == 0
    err = capsys.readouterr().err
    assert "skill install skipped (agents-cli setup exited 1)" in err
    assert "npx: command not found" in err


def test_a_failed_skill_install_does_not_fail_deploy(failing_setup, capsys):
    assert acli_deploy.main(["--deploy-aqua"]) == 0
    err = capsys.readouterr().err
    assert "skill install skipped (agents-cli setup exited 1)" in err
    assert "npx: command not found" in err


def test_an_unlaunchable_setup_does_not_fail_the_install(
    wrappers, monkeypatch, capsys
):
    def _raise_oserror(*args: Any, **kwargs: Any) -> NoReturn:
        raise OSError("exec format error")

    monkeypatch.setattr(_acli, "run", _raise_oserror)
    _acli.install_skill_best_effort(Path.cwd())
    assert (
        "skill install skipped (exec format error)" in capsys.readouterr().err
    )


def test_a_source_without_skill_md_runs_no_setup(
    wrappers, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        _acli, "resolve_extension_root", lambda: tmp_path / "empty"
    )
    _acli.install_skill_best_effort(tmp_path)
    assert _skill_install_calls(wrappers) == []
    assert "skill install skipped (no SKILL.md" in capsys.readouterr().err
