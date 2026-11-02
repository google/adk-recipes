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

"""make setup-infra (``infra/setup_infra.sh``) and the ``config.env``
target, with a fake terraform and gcloud; the agents-cli settings."""

import os
import shutil
import tomllib
from pathlib import Path

import pytest

from tests.infra.setup_infra_fakes import calls, make_fakes, run

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra" / "setup_infra.sh"
STATE = "my-cherry-genmedia-tfstate"
MEDIA = "my-cherry-genmedia-for-commerce-media-payloads"
ASK = "Apply these changes to project my-cherry? [Y/n]"


@pytest.fixture
def setup(tmp_path):
    """Run the script as make setup-infra does: (code, output, calls)."""
    env = make_fakes(tmp_path) | {
        "TF_VAR_project_id": "my-cherry",
        "TF_VAR_region": "europe-west1",
        "TF_VAR_service_name": "my-svc",
    }

    def go(*args: str, answer=None, **fake: str):
        code, out = run(
            ["bash", str(SCRIPT), *args], env | fake, tmp_path, answer
        )
        return code, out, calls(tmp_path)

    return go


def _imports(made: list[str]) -> list[str]:
    return [c for c in made if c.startswith("terraform import")]


def test_the_state_bucket_is_created_when_missing(setup):
    code, _, made = setup("init")

    assert code == 0
    assert (
        f"gcloud storage buckets create gs://{STATE} --project=my-cherry "
        "--location=europe-west1 --uniform-bucket-level-access "
        "--public-access-prevention --quiet" in made
    )
    assert (
        f"gcloud storage buckets update gs://{STATE} --versioning --quiet"
        in made
    )
    assert (
        f"terraform init -input=false -reconfigure -backend-config=bucket={STATE}"
        in made
    )
    assert "terraform state list" in made  # init also adopts (make tf-plan)
    assert not [c for c in made if c.startswith("terraform plan")]


def test_an_existing_state_bucket_is_used(setup):
    code, _, made = setup("init", FAKE_EXISTING=STATE)

    assert code == 0
    assert not [c for c in made if "buckets create" in c]
    assert any(c.startswith("terraform init") for c in made)


def test_only_what_exists_and_is_not_in_the_state_is_imported(setup):
    code, _, made = setup(
        TF_AUTO_APPROVE="1",
        FAKE_EXISTING=f"{STATE} {MEDIA} genmedia-build genmedia-web my-svc-repo",
        FAKE_STATE="google_service_account.web",
    )

    assert code == 0
    assert _imports(made) == [
        f"terraform import -input=false google_storage_bucket.media {MEDIA}",
        "terraform import -input=false google_service_account.build "
        "projects/my-cherry/serviceAccounts/"
        "genmedia-build@my-cherry.iam.gserviceaccount.com",
        "terraform import -input=false "
        "google_artifact_registry_repository.docker "
        "projects/my-cherry/locations/europe-west1/repositories/my-svc-repo",
    ]
    assert made.index(_imports(made)[-1]) < made.index(
        next(c for c in made if c.startswith("terraform plan"))
    )


def test_a_failed_import_stops_before_the_plan(setup):
    code, _, made = setup(
        TF_AUTO_APPROVE="1",
        FAKE_EXISTING=f"{STATE} {MEDIA}",
        FAKE_IMPORT="1",
    )

    assert code != 0
    assert len(_imports(made)) == 1
    assert not [c for c in made if c.startswith("terraform plan")]


def test_nothing_is_imported_in_a_new_project(setup):
    code, _, made = setup(TF_AUTO_APPROVE="1")

    assert code == 0
    assert not _imports(made)


def _applies(made: list[str]) -> list[str]:
    return [c for c in made if c.startswith("terraform apply")]


def test_without_a_terminal_nothing_is_applied(setup):
    code, out, made = setup()

    assert code == 1
    assert "set TF_AUTO_APPROVE=1 to apply it without asking" in out
    assert not _applies(made)


def test_without_a_terminal_auto_approve_applies_the_plan(setup):
    code, out, made = setup(TF_AUTO_APPROVE="1")

    assert code == 0
    assert ASK not in out
    plan = next(c for c in made if c.startswith("terraform plan"))
    plan_file = plan.split("-out=")[1]
    assert _applies(made) == [f"terraform apply -input=false {plan_file}"]
    assert not os.path.exists(plan_file)  # removed at the end


@pytest.mark.parametrize("answer", ["\n", "y\n", "yes\n", "Y\n"])
def test_on_a_terminal_enter_or_yes_applies(setup, answer):
    code, out, made = setup(answer=answer)

    assert code == 0, out
    assert ASK in out
    assert len(_applies(made)) == 1


def test_on_a_terminal_no_changes_nothing(setup):
    code, out, made = setup(answer="n\n")

    assert code == 1
    assert "Nothing changed." in out
    assert not _applies(made)


def test_a_failed_apply_is_planned_and_applied_again_without_asking(setup):
    code, out, made = setup(answer="\n", FAKE_APPLY="1 0")

    assert code == 0, out
    assert out.count(ASK) == 1
    assert len(_applies(made)) == 2
    assert len([c for c in made if c.startswith("terraform plan")]) == 2
    assert made.count("sleep 30") == 1
    assert "Terraform failed (attempt 1 of 3)" in out


def test_three_failures_stop_with_an_error(setup):
    code, _, made = setup(TF_AUTO_APPROVE="1", FAKE_APPLY="1")

    assert code == 1
    assert len(_applies(made)) == 3
    assert made.count("sleep 30") == 2


def test_no_changes_ask_nothing(setup):
    code, out, made = setup(answer="", FAKE_PLAN="0")

    assert code == 0
    assert ASK not in out
    assert not _applies(made)


# --------------------------------------------- config.env from gcloud


@pytest.fixture
def make_config(tmp_path):
    """make config.env in a folder with config.env.example only."""
    shutil.copy(ROOT / "config.env.example", tmp_path)
    env = make_fakes(tmp_path)

    def go(answer=None, project="my-cherry"):
        command = ["make", "-s", "-f", str(ROOT / "Makefile"), "config.env"]
        fake = {"FAKE_PROJECT": project}
        code, out = run(command, env | fake, tmp_path, answer)
        config = tmp_path / "config.env"
        return code, out, config.read_text() if config.exists() else None

    return go


QUESTION = "Deploy to project my-cherry (gcloud's active project)? [Y/n]"
needs_make = pytest.mark.skipif(not shutil.which("make"), reason="no make")


@needs_make
@pytest.mark.parametrize("answer", ["\n", "yes\n"])
def test_enter_or_yes_uses_gcloud_s_project(make_config, answer):
    code, out, config = make_config(answer)

    assert code == 0, out
    assert QUESTION in out
    assert "PROJECT_ID=my-cherry\n" in config


@needs_make
def test_no_stops_and_says_how_to_set_the_project(make_config):
    code, out, config = make_config("n\n")

    assert code != 0
    assert (
        "Set the project: gcloud config set project <id> (or create "
        "config.env from config.env.example)." in out
    )
    assert config is None


@needs_make
def test_without_a_terminal_the_project_is_used_and_named(make_config):
    code, out, config = make_config()

    assert code == 0
    assert "Using project my-cherry (gcloud's active project)." in out
    assert QUESTION not in out
    assert "PROJECT_ID=my-cherry\n" in config


@needs_make
def test_no_gcloud_project_is_an_error(make_config):
    code, out, config = make_config("\n", project="")

    assert code != 0
    assert "no config.env and no gcloud project" in out
    assert config is None


# ------------------------------------------------------- pyproject.toml


def test_pyproject_has_agent_starter_pack_and_agents_cli_settings():
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]

    assert tool["agent-starter-pack"]["base_template"] == "adk_base"
    assert tool["agents-cli"]["base_template"] == "empty_py"
    assert tool["agents-cli"]["settings"] == {
        "agent_directory": "genmedia4commerce",
        "deployment_targets": ["cloud_run"],
    }
