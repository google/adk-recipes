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

"""`make onboard` (scripts/onboard.py), `make deploy`'s computer image and secrets, and
`make doctor`."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from app.app_utils.http_client import set_default_http_transport
from app.auth.env_store import dotenv_path, read_dotenv
from scripts import deploy, onboard
from scripts.provisioning import github_app, sandbox_image

ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "scripts" / "doctor.sh"
REPO = "us-central1-docker.pkg.dev/p/adk-voice-agent"
APP_URL = "https://adk-voice-agent-123.us-central1.run.app"
ENGINE = "projects/p/locations/us-central1/reasoningEngines/42"
GOOGLE_KEYS = ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")
GITHUB_KEYS = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_APP_SLUG")


# ---- the one question ---------------------------------------------------------


def test_without_a_terminal_it_takes_the_default_and_never_reads_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", pytest.fail)
    assert (
        onboard.ask_project("from-gcloud", interactive=False) == "from-gcloud"
    )


def test_without_a_terminal_or_a_default_it_fails() -> None:
    with pytest.raises(SystemExit, match="no GCP project"):
        onboard.ask_project(None, interactive=False)


def test_in_a_terminal_the_answer_wins_and_enter_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "typed")
    assert onboard.ask_project("default", interactive=True) == "typed"
    monkeypatch.setattr("builtins.input", lambda _: "  ")
    assert onboard.ask_project("default", interactive=True) == "default"


def test_in_a_terminal_non_interactive_flags_never_read_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onboard.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", pytest.fail)
    monkeypatch.setattr(onboard.ui.getpass, "getpass", pytest.fail)

    assert onboard.ui.is_interactive(no_input=True) is False
    with pytest.raises(SystemExit, match="--no-input"):
        onboard.ui.ask("Project", no_input=True)
    assert onboard.ui.ask("Project", default="p", no_input=True) == "p"
    with pytest.raises(SystemExit, match="--no-input"):
        onboard.ui.ask_secret("Secret", no_input=True)
    assert onboard.ui.confirm("Create?", default=True, no_input=True) is False

    monkeypatch.setenv("NONINTERACTIVE", "1")
    assert onboard.ui.is_interactive() is False
    assert onboard.ui.ask("Project", default="p") == "p"
    with pytest.raises(SystemExit, match="NONINTERACTIVE=1"):
        onboard.ui.ask_secret("Secret")
    assert onboard.ui.confirm("Create?", default=True) is False
    with pytest.raises(SystemExit, match="no GCP project"):
        onboard.ask_project(None, interactive=onboard.ui.is_interactive())


def test_the_project_is_saved_to_a_dotenv_made_from_the_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = tmp_path / ".env.example"
    example.write_text(
        "GOOGLE_CLOUD_PROJECT=your-gcp-project-id\nLIVE_MODEL=\n"
    )
    monkeypatch.setattr(onboard, "ENV_EXAMPLE_FILE", example)
    monkeypatch.setenv("VOICE_AGENT_DOTENV_PATH", str(tmp_path / ".env"))

    onboard.save_project("my-project")

    saved = dotenv_values(tmp_path / ".env")
    assert saved["GOOGLE_CLOUD_PROJECT"] == "my-project"
    assert "LIVE_MODEL" in saved  # the rest of the template came along


# ---- the whole run ----------------------------------------------------------------


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    recorded: list[list[str]] = []
    monkeypatch.setattr(onboard.shutil, "which", lambda tool: f"/bin/{tool}")
    monkeypatch.setattr(
        onboard.deploy,
        "terraform_output",
        lambda name: ENGINE if name == "agent_engine" else None,
    )
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")
    monkeypatch.setattr(onboard, "save_project", lambda project: None)
    monkeypatch.setattr(onboard, "run_quiet", lambda cmd, timeout: "adc-token")
    monkeypatch.setattr(onboard, "run", recorded.append)
    monkeypatch.setattr(onboard.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        onboard, "deployed_app_url", lambda project, region, var_file: APP_URL
    )
    monkeypatch.setattr(
        onboard,
        "register_apps",
        lambda project, app_url, **flags: recorded.append(
            ["register", app_url, *sorted(k for k, v in flags.items() if v)]
        ),
    )
    monkeypatch.setattr(onboard, "default_tag", lambda: "abc1234")
    monkeypatch.setattr(
        onboard.deploy,
        "terraform_console",
        lambda expr, project, region, var_file: REPO,
    )
    monkeypatch.setattr(
        onboard,
        "ensure_image",
        lambda project, region, tag, repository: recorded.append(
            ["build", f"{repository}/computer:{tag}"]
        ),
    )
    return recorded


def test_onboard_applies_builds_the_pinned_image_then_deploys(
    steps: list[list[str]],
) -> None:
    onboard.main(["--project=p"])

    init, register, apply, build, ship = steps
    assert init[-2:] == ["init", "-input=false"]
    assert register == ["register", APP_URL]
    image = f"{REPO}/computer:abc1234"
    assert apply[2:5] == ["apply", "-input=false", "-auto-approve"]
    assert f"-var=computer_image={image}" in apply
    assert "-var=project_id=p" in apply
    assert not any("enable_workspace_apis" in arg for arg in apply)
    assert build == ["build", image]
    assert (
        dotenv_values(dotenv_path())["GOOGLE_CLOUD_AGENT_ENGINE_ID"] == ENGINE
    )
    assert ship[1].endswith("scripts/deploy.py")
    assert "--project=p" in ship
    assert "--dry-run" not in ship


def test_onboard_dry_run_plans_and_builds_nothing(
    steps: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard, "save_project", pytest.fail)
    onboard.main(["--project=p", "--dry-run", "--var-file=prod.tfvars"])

    _init, plan, ship = steps  # registers no app
    assert plan[2] == "plan"
    assert "-auto-approve" not in plan
    assert "-var-file=prod.tfvars" in plan
    assert ship[-2:] == ["--var-file=prod.tfvars", "--dry-run"]
    assert read_dotenv() == {}


def test_onboard_no_input_skips_prompts_even_in_a_terminal(
    steps: list[list[str]], monkeypatch: pytest.MonkeyPatch, no_input: None
) -> None:
    monkeypatch.setattr(onboard.sys.stdin, "isatty", lambda: True)
    onboard.main(["--project=p", "--no-input"])

    _init, register, _apply, _build, _ship = steps
    assert register == ["register", APP_URL]


def test_onboard_needs_application_default_credentials(
    steps: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard, "run_quiet", lambda cmd, timeout: None)
    with pytest.raises(SystemExit, match="application-default login"):
        onboard.main(["--project=p"])
    assert steps == []


def test_skip_flags_come_from_the_environment_or_the_command_line(
    steps: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKIP_GOOGLE", "1")
    monkeypatch.setenv("GITHUB_ORG", "acme")
    onboard.main(["--project=p", "--skip-github"])
    assert steps[1] == [
        "register",
        APP_URL,
        "github_org",
        "skip_github",
        "skip_google",
    ]


# ---- connected apps -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def gcloud_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suggested consent-screen email, without calling gcloud."""
    monkeypatch.delenv("NONINTERACTIVE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        onboard.deploy, "gcloud_account", lambda: "me@example.com"
    )


@pytest.fixture
def no_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails the test if onboarding reads stdin."""
    monkeypatch.setattr("builtins.input", pytest.fail)
    monkeypatch.setattr(onboard.ui.getpass, "getpass", pytest.fail)


def test_google_redirect_uris_are_the_deployment_and_make_dev() -> None:
    assert onboard.redirect_uris("google_workspace", APP_URL) == [
        f"{APP_URL}/api/v1/auth/google_workspace/callback",
        "http://127.0.0.1:8000/api/v1/auth/google_workspace/callback",
    ]


def test_without_a_terminal_an_unset_app_is_skipped(
    no_input: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert onboard.register_google("p", APP_URL, {}, interactive=False) == {}
    assert onboard.register_github(APP_URL, {}, interactive=False) == {}
    out = capsys.readouterr().out
    assert "Skipped: set GOOGLE_OAUTH_CLIENT_ID" in out
    assert "Skipped: set GITHUB_CLIENT_ID" in out


def test_keys_already_set_are_kept_without_asking(
    no_input: None, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {k: f"{k.lower()}-value" for k in (*GOOGLE_KEYS, *GITHUB_KEYS)}
    assert onboard.register_google("p", APP_URL, env, interactive=True) == {
        k: env[k] for k in GOOGLE_KEYS
    }
    assert onboard.register_github(APP_URL, env, interactive=True) == {
        k: env[k] for k in GITHUB_KEYS
    }
    out = capsys.readouterr().out
    # Reminds you what the existing client and App must list, and prints no secret.
    assert f"{APP_URL}/api/v1/auth/google_workspace/callback" in out
    assert f"{APP_URL}/api/v1/auth/github/callback" in out
    assert "google_oauth_client_secret-value" not in out


def test_google_prints_the_console_link_and_asks_for_the_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "builtins.input", lambda _: " 123-abc.apps.googleusercontent.com "
    )
    monkeypatch.setattr(
        onboard.ui.getpass, "getpass", lambda _: "client-secret"
    )

    saved = onboard.register_google("my-proj", APP_URL, {}, interactive=True)

    assert saved == dict(
        zip(
            GOOGLE_KEYS,
            ("123-abc.apps.googleusercontent.com", "client-secret"),
            strict=True,
        )
    )
    out = capsys.readouterr().out
    assert (
        "https://console.cloud.google.com/apis/credentials?project=my-proj"
        in out
    )
    assert "Web application" in out
    assert "this project, my-proj" in out
    assert "Name: ADK Voice Agent" in out
    assert f"{APP_URL}/api/v1/auth/google_workspace/callback" in out
    assert "http://127.0.0.1:8000/api/v1/auth/google_workspace/callback" in out


def test_a_mistyped_google_client_id_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(
        ["GOCSPX-pasted-the-secret", "9-x.apps.googleusercontent.com"]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(
        onboard.ui.getpass, "getpass", lambda _: "client-secret"
    )

    saved = onboard.register_google("p", APP_URL, {}, interactive=True)

    assert saved[GOOGLE_KEYS[0]] == "9-x.apps.googleusercontent.com"
    assert "ends with .apps.googleusercontent.com" in capsys.readouterr().out


def test_enter_skips_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(onboard.ui.getpass, "getpass", pytest.fail)
    assert onboard.register_google("p", APP_URL, {}, interactive=True) == {}


def test_github_creates_the_app_unless_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = dict(
        zip(
            GITHUB_KEYS,
            ("Iv1.abc", "s3cret", "adk-voice-agent-123"),
            strict=True,
        )
    )
    calls: list[tuple[str | None, str | None]] = []

    def fake_create(app_url: str | None, *, org: str | None = None) -> dict:
        calls.append((app_url, org))
        return created

    monkeypatch.setattr(onboard, "create_github_app", fake_create)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert (
        onboard.register_github(APP_URL, {}, interactive=True, org="acme")
        == created
    )
    assert calls == [(APP_URL, "acme")]

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert onboard.register_github(APP_URL, {}, interactive=True) == {}
    assert len(calls) == 1


def test_a_failed_github_app_creation_skips_github(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(app_url: str | None, *, org: str | None = None) -> dict:
        raise ValueError("no answer from GitHub within 600 s.")

    monkeypatch.setattr(onboard, "create_github_app", fail)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert onboard.register_github(APP_URL, {}, interactive=True) == {}
    assert "❌ GitHub: no answer from GitHub" in capsys.readouterr().out


def test_keys_from_the_shell_are_saved_to_dotenv_for_deploy(
    monkeypatch: pytest.MonkeyPatch, no_input: None
) -> None:
    for key, value in zip(
        GOOGLE_KEYS, ("from-shell-id", "from-shell-secret"), strict=True
    ):
        monkeypatch.setenv(key, value)

    onboard.register_apps("p", APP_URL, interactive=False, skip_github=True)

    saved = read_dotenv()
    assert saved["GOOGLE_OAUTH_CLIENT_ID"] == "from-shell-id"
    assert saved["GOOGLE_OAUTH_CLIENT_SECRET"] == "from-shell-secret"
    assert not any(k in saved for k in GITHUB_KEYS)


def test_skipped_apps_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, no_input: None
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    onboard.register_apps(
        "p", APP_URL, interactive=True, skip_google=True, skip_github=True
    )
    assert read_dotenv() == {}


# ---- make deploy's secrets -----------------------------------------------------------


def test_service_url_matches_cloud_run() -> None:
    assert (
        deploy.service_url("123", "us-central1", "adk-voice-agent") == APP_URL
    )


def test_app_keys_get_one_secret_each() -> None:
    """terraform/secrets.tf stores every app's OAuth client (from integrations.yaml) and
    the GitHub App's slug, which isn't in integrations.yaml, one secret per key."""
    secrets_tf = (ROOT / "terraform" / "secrets.tf").read_text()
    assert '"client_id_env"' in secrets_tf
    assert '"client_secret_env"' in secrets_tf
    assert '"GITHUB_APP_SLUG"' in secrets_tf
    assert "voice-agent-deployment" not in secrets_tf


def test_terraform_scopes_per_instance_iam_and_deployment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth.credential_store import user_secret_id

    computer_tf = (ROOT / "terraform" / "computer.tf").read_text()
    iam_tf = (ROOT / "terraform" / "iam.tf").read_text()
    variables_tf = (ROOT / "terraform" / "variables.tf").read_text()
    outputs_tf = (ROOT / "terraform" / "outputs.tf").read_text()

    manager = re.search(
        r'resource "google_project_iam_custom_role" "computer_manager" \{(.*?)\n\}',
        computer_tf,
        re.S,
    )
    assert manager
    manager_perms = set(
        re.findall(r'"([a-z]+\.[A-Za-z]+\.[A-Za-z]+)"', manager.group(1))
    )
    assert {p for p in manager_perms if p.startswith("compute.instances.")} == {
        "compute.instances.create",
        "compute.instances.list",
    }
    assert not any(p.startswith("iap.tunnelInstances.") for p in manager_perms)

    instance_binding = re.search(
        r'resource "google_project_iam_member" "app_computer_instance_manager" \{(.*?)\n\}',
        computer_tf,
        re.S,
    )
    assert instance_binding
    assert (
        'resource.name.extract(\\"/instances/{name}\\").startsWith(\\"${var.service_name}-u-\\")'
        in instance_binding.group(1)
    )

    assert "coalesce(var.service_account_id, var.service_name)" in iam_tf
    assert (
        'coalesce(var.computer_service_account_id, "${var.service_name}-computer")'
        in computer_tf
    )
    assert "^[a-z][-a-z0-9]{4,28}[a-z0-9]$" in variables_tf
    assert "local.service_account_id" in outputs_tf
    assert "local.computer_service_account_id" in outputs_tf
    assert "/secrets/${var.service_name}-user-" in iam_tf

    monkeypatch.delenv("COMPUTER_DEPLOYMENT", raising=False)
    assert user_secret_id("alice@example.com").startswith("voice-agent-user-")
    monkeypatch.setenv("COMPUTER_DEPLOYMENT", "voice-staging")
    assert user_secret_id("alice@example.com").startswith("voice-staging-user-")


def test_sync_instance_template_updates_cloud_run_before_image_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    deploy.sync_instance_template(
        "p", "us-central1", "adk-voice-agent", "tmpl-v2", dry_run=True
    )
    assert calls == []

    deploy.sync_instance_template(
        "p", "us-central1", "adk-voice-agent", "tmpl-v2", dry_run=False
    )
    assert calls == [
        [
            "gcloud",
            "run",
            "services",
            "update",
            "adk-voice-agent",
            "--project=p",
            "--region=us-central1",
            "--update-env-vars=COMPUTER_INSTANCE_TEMPLATE=tmpl-v2",
            "--quiet",
        ]
    ]


def test_cloud_sql_and_ssh_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import computer_ssh

    cloud_sql_tf = (ROOT / "terraform" / "cloud_sql.tf").read_text()
    iam_tf = (ROOT / "terraform" / "iam.tf").read_text()
    assert "point_in_time_recovery_enabled = true" in cloud_sql_tf
    assert "final_backup_config" in cloud_sql_tf
    assert "var.grant_cloud_build_builder ? 1 : 0" in iam_tf

    exec_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        computer_ssh, "gcloud_account", lambda: "me@example.com"
    )
    monkeypatch.setattr(
        computer_ssh, "deploy_config", lambda *args: DEPLOY_CONFIG
    )
    monkeypatch.setattr(
        computer_ssh, "instance_status", lambda *args: "RUNNING"
    )
    monkeypatch.setattr(
        computer_ssh.os,
        "execvp",
        lambda file, args: exec_calls.append((file, list(args))),
    )
    computer_ssh.main(["--project=p", "--", "echo", "hi"])
    assert exec_calls and "--quiet" in exec_calls[0][1]


def test_deploy_stores_changed_values_and_mounts_every_secret_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = {"adk-github-client-id": "Iv1.old", "adk-github-app-slug": "slug"}
    added: list[tuple[str, str]] = []

    def fake_secret(project: str, *args: str, value: str | None = None):
        if args[:2] == ("versions", "access"):
            name = args[-1].removeprefix("--secret=")
            found = name in store
            return subprocess.CompletedProcess(
                args, 0 if found else 1, stdout=store.get(name, ""), stderr=""
            )
        added.append((args[2], value or ""))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(deploy, "_gcloud_secret", fake_secret)
    secrets = {
        "GITHUB_CLIENT_ID": "adk-github-client-id",
        "GITHUB_APP_SLUG": "adk-github-app-slug",
        "GOOGLE_OAUTH_CLIENT_ID": "adk-google-oauth-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "adk-google-oauth-client-secret",
    }
    env = {"GITHUB_CLIENT_ID": "Iv1.new", "GOOGLE_OAUTH_CLIENT_ID": "g-id"}

    mounted = deploy.sync_credentials("p", secrets, env, dry_run=False)

    assert sorted(added) == [
        ("adk-github-client-id", "Iv1.new"),
        ("adk-google-oauth-client-id", "g-id"),
    ]
    # The slug is already stored, so it's deployed even though `.env` lacks it; the
    # Google secret has no value anywhere, so it isn't.
    assert mounted == {
        "GITHUB_CLIENT_ID": "adk-github-client-id",
        "GITHUB_APP_SLUG": "adk-github-app-slug",
        "GOOGLE_OAUTH_CLIENT_ID": "adk-google-oauth-client-id",
    }


def test_deploy_ships_only_keys_env_example_declares(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keys an older version left in `.env` stay local; only their names are printed."""
    env: dict[str, str | None] = {
        "LIVE_MODEL": "gemini-live",
        "COMPUTER_PROVIDER": "",
        "SPOTIFY_REFRESH_TOKEN": "spotify-secret",
        "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret",
    }

    assert deploy.declared_env(env) == {"LIVE_MODEL": "gemini-live"}
    out = capsys.readouterr().out
    assert "GITHUB_PERSONAL_ACCESS_TOKEN, SPOTIFY_REFRESH_TOKEN" in out
    assert "secret" not in out


DEPLOY_CONFIG = {
    "computer_zone": "us-central1-a",
    "computer_network": "voice-staging-net",
    "computer_deployment": "voice-staging",
    "computer_service_account_email": "computer@p.iam.gserviceaccount.com",
    "computer_snapshot_policy": "projects/p/regions/r/resourcePolicies/s",
}


def test_deploy_names_the_computers_after_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy, "terraform_output", lambda name: "tmpl")
    env = deploy.computer_env(DEPLOY_CONFIG, APP_URL, dry_run=False)
    assert env["COMPUTER_DEPLOYMENT"] == "voice-staging"


def test_deploy_sets_the_memory_engine_and_registers_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import store

    registered: list[str] = []
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")
    monkeypatch.setattr(deploy, "terraform_output", lambda name: ENGINE)

    def fake_ensure_schema() -> str:
        registered.append(os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ID"])
        return "registered"

    monkeypatch.setattr(store, "ensure_profile_schema", fake_ensure_schema)
    engine = deploy.memory_engine(dry_run=False)
    deploy.register_memory_schema(engine)
    assert engine == ENGINE and registered == [ENGINE]
    # .env's own value never reaches the service; deploy sets it from terraform.
    assert "GOOGLE_CLOUD_AGENT_ENGINE_ID" in deploy.LOCAL_ONLY_KEYS


def test_deploy_needs_the_memory_engine_except_in_a_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy, "terraform_output", lambda name: None)
    assert deploy.memory_engine(dry_run=True) == "<agent engine>"
    with pytest.raises(SystemExit):
        deploy.memory_engine(dry_run=False)


@pytest.mark.parametrize("purge", [False, True])
def test_destroy_deletes_only_this_deployments_computers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    purge: bool,
) -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "found-1\n", "")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    deploy.delete_computers("p", DEPLOY_CONFIG, purge=purge)

    filters = {
        argv[2]: next(a for a in argv if a.startswith("--filter="))
        for argv in calls
        if argv[3] == "list"
    }
    assert sorted(filters) == ["disks", "instances"]
    for filter_ in filters.values():
        assert "labels.deployment=voice-staging" in filter_
        assert "zone:us-central1-a" in filter_
    actions = [(argv[2], argv[3]) for argv in calls if argv[3] != "list"]
    if purge:
        assert actions == [("instances", "delete"), ("disks", "delete")]
    else:
        assert actions == [
            ("instances", "delete"),
            ("disks", "remove-resource-policies"),
        ]
        assert calls[-1] == [
            "gcloud",
            "compute",
            "disks",
            "remove-resource-policies",
            "found-1",
            f"--resource-policies={DEPLOY_CONFIG['computer_snapshot_policy']}",
            "--project=p",
            "--zone=us-central1-a",
            "--quiet",
        ]
        assert "without scheduled snapshots" in capsys.readouterr().out


def test_the_computer_image_is_built_for_both_architectures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[list[str]] = []
    monkeypatch.setattr(sandbox_image, "gcloud", lambda *args: "")
    monkeypatch.setattr(
        sandbox_image.subprocess, "run", lambda cmd, check: builds.append(cmd)
    )

    uri = sandbox_image.build_image("p", "us-central1", "abc1234", REPO)

    assert uri == f"{REPO}/computer:abc1234"
    (build,) = builds
    assert build[:4] == ["gcloud", "builds", "submit", str(ROOT / "sandbox")]
    assert f"--config={ROOT / 'sandbox' / 'cloudbuild.yaml'}" in build
    assert f"--substitutions=_IMAGE={uri}" in build
    assert not any(arg.startswith("--tag") for arg in build)


# ---- make deploy's computer image -------------------------------------------------


@pytest.fixture
def terraform_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """`var.computer_image` (the tfvars pin) and the applied `computer_image` output."""
    state = {"pinned": "", "applied": ""}
    monkeypatch.setattr(
        deploy,
        "terraform_console",
        lambda expr, *args: (
            state["pinned"] if expr == "var.computer_image" else None
        ),
    )
    monkeypatch.setattr(
        deploy, "terraform_output", lambda name: state["applied"] or None
    )
    return state


def test_an_asked_for_image_wins(terraform_state: dict[str, str]) -> None:
    terraform_state.update(pinned="pin:1", applied="old:1")
    assert deploy.computer_image("p", "r", requested="new:2") == "new:2"


def test_a_tfvars_pin_comes_next(terraform_state: dict[str, str]) -> None:
    terraform_state.update(pinned="pin:1", applied="old:1")
    assert deploy.computer_image("p", "r") == "pin:1"


def test_an_unpinned_deploy_keeps_the_running_image(
    terraform_state: dict[str, str],
) -> None:
    terraform_state.update(applied=f"{REPO}/computer:abc1234")
    assert deploy.computer_image("p", "r") == f"{REPO}/computer:abc1234"


@pytest.mark.parametrize("applied", ["", f"{REPO}/computer:latest"])
def test_it_never_falls_back_to_latest(
    terraform_state: dict[str, str], applied: str
) -> None:
    terraform_state.update(applied=applied)
    with pytest.raises(SystemExit, match="make computer-image"):
        deploy.computer_image("p", "r")


def test_terraform_vars_match_make_deploy() -> None:
    assert deploy.terraform_vars("p", "r", "x.tfvars", "img:1") == [
        "-var=project_id=p",
        "-var=region=r",
        "-var-file=x.tfvars",
        "-var=computer_image=img:1",
    ]


# ---- make doctor ------------------------------------------------------------------

# Stands in for curl inside a computer whose checks all pass.
_FAKE_CURL = """\
import sys
args = sys.argv[1:]
url = args[-1]
if "/mcp-relay/" in url:
    out = args[args.index("-o") + 1]
    open(out, "w").write('{"detail": "Missing ADK Voice Agent MCP token."}')
    print("401", end="")
elif url.endswith("/email"):
    print("computer@p.iam.gserviceaccount.com")
elif url.endswith("/token"):
    print('{"access_token": "t"}')
elif "testIamPermissions" in url:
    print('{"permissions": ["aiplatform.endpoints.predict"]}')
"""


def _computer(tmp_path: Path, curl: str = _FAKE_CURL) -> dict[str, str]:
    """PATH with the computer's tools faked, and the env doctor.sh runs with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "claude",
        "agy",
        "gh",
        "git",
        "node",
        "uv",
        "mcp-remote",
    ):
        stub = bin_dir / tool
        stub.write_text(f"#!/bin/sh\necho '{tool} 1.0.0'\n")
        stub.chmod(0o755)
    (bin_dir / "timeout").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (bin_dir / "curl.py").write_text(curl)
    (bin_dir / "curl").write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "curl.py"}" "$@"\n'
    )
    for name in ("timeout", "curl"):
        (bin_dir / name).chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GOOGLE_CLOUD_PROJECT": "p",
        "DOCTOR_MIN_FREE_MB": "1",
    }


def test_doctor_is_posix_sh_and_executable() -> None:
    check = subprocess.run(
        ["/bin/sh", "-n", str(DOCTOR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    assert os.access(DOCTOR, os.X_OK)


@pytest.mark.skipif(
    not shutil.which("jq"), reason="doctor.sh uses jq (in the image)"
)
def test_doctor_passes_on_a_healthy_computer(tmp_path: Path) -> None:
    result = subprocess.run(
        ["sh", "-s"], stdin=DOCTOR.open(), env=_computer(tmp_path),
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    for line in (
        "✓ claude         claude 1.0.0",
        "✓ vertex         may call models in p",
        "✓ bridge         the relay reaches the app's MCP bridge",
        "All checks passed.",
    ):
        assert line in result.stdout


@pytest.mark.skipif(
    not shutil.which("jq"), reason="doctor.sh uses jq (in the image)"
)
def test_doctor_reports_an_unreachable_bridge(tmp_path: Path) -> None:
    curl = _FAKE_CURL.replace('print("401", end="")', 'print("502", end="")')
    result = subprocess.run(
        ["sh", "-s"], stdin=DOCTOR.open(), env=_computer(tmp_path, curl),
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 1
    assert "✗ bridge         HTTP 502" in result.stdout


# ---- make onboard's GitHub App creation (scripts/provisioning/github_app.py) -----

GITHUB_APP_URL = "https://adk-voice-agent-123456789012.us-central1.run.app"
REDIRECT = "http://127.0.0.1:5555/created"


def test_the_manifest_asks_for_what_threads_need_and_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    manifest = github_app.app_manifest(GITHUB_APP_URL, REDIRECT)

    assert manifest["url"] == GITHUB_APP_URL
    assert manifest["redirect_url"] == REDIRECT
    assert manifest["callback_urls"] == [
        f"{GITHUB_APP_URL}/api/v1/auth/github/callback",
        "http://127.0.0.1:8000/api/v1/auth/github/callback",
    ]
    assert manifest["request_oauth_on_install"] is True
    assert manifest["public"] is True
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
    }
    assert manifest["default_events"] == []
    assert "hook_attributes" not in manifest


def test_the_name_fits_github_and_keeps_what_tells_deployments_apart() -> None:
    name = github_app.app_name(GITHUB_APP_URL)
    assert len(name) <= github_app.MAX_APP_NAME
    assert name.startswith("ADK Voice Agent (")
    assert name.endswith("123456789012)")
    assert github_app.app_name("https://x.run.app") == "ADK Voice Agent (x)"


def test_without_a_deployment_the_manifest_is_for_make_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    manifest = github_app.app_manifest(None, REDIRECT)
    assert manifest["url"] == "http://127.0.0.1:8000"
    assert manifest["callback_urls"] == [
        "http://127.0.0.1:8000/api/v1/auth/github/callback"
    ]


def test_the_app_is_created_under_your_account_or_an_organization() -> None:
    assert (
        github_app.new_app_url("s1")
        == "https://github.com/settings/apps/new?state=s1"
    )
    assert (
        github_app.new_app_url("s1", "acme")
        == "https://github.com/organizations/acme/settings/apps/new?state=s1"
    )
    with pytest.raises(ValueError, match="organization name"):
        github_app.new_app_url("s1", "../evil")


def test_the_callback_needs_this_runs_state_and_a_well_formed_code() -> None:
    assert github_app.parse_callback("code=abc_123&state=s1", "s1") == "abc_123"
    with pytest.raises(ValueError, match="state"):
        github_app.parse_callback("code=abc&state=other", "s1")
    with pytest.raises(ValueError, match="state"):
        github_app.parse_callback("code=abc", "s1")
    with pytest.raises(ValueError, match="code"):
        github_app.parse_callback("state=s1", "s1")
    with pytest.raises(ValueError, match="code"):
        github_app.parse_callback("code=a%2Fb&state=s1", "s1")


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_the_local_server_posts_the_manifest_then_catches_the_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    catcher = github_app.ManifestCatcher(GITHUB_APP_URL, org="acme")
    pages: dict[str, str] = {}

    def browser(url: str) -> None:
        _status, pages["form"] = _get(url)
        action = re.search(r'action="([^"]+)"', pages["form"])
        assert action
        state = html.unescape(action.group(1)).split("state=", 1)[1]
        redirect = catcher.manifest["redirect_url"]
        status, pages["done"] = _get(f"{redirect}?code=the-code&state={state}")
        assert status == 200

    assert catcher.wait(open_browser=browser, timeout=10) == "the-code"
    form = pages["form"]
    assert (
        "https://github.com/organizations/acme/settings/apps/new?state=" in form
    )
    value = re.search(r'name="manifest" value="([^"]+)"', form)
    assert value
    assert json.loads(html.unescape(value.group(1))) == catcher.manifest
    assert catcher.manifest["redirect_url"].startswith("http://127.0.0.1:")
    assert "return to the terminal" in pages["done"]


def test_a_redirect_with_the_wrong_state_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    catcher = github_app.ManifestCatcher(None)

    def browser(url: str) -> None:
        status, _ = _get(
            f"{catcher.manifest['redirect_url']}?code=c&state=forged"
        )
        assert status == 400

    with pytest.raises(ValueError, match="state"):
        catcher.wait(open_browser=browser, timeout=10)


def test_no_answer_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REDIRECT_URI", raising=False)
    catcher = github_app.ManifestCatcher(None)
    with pytest.raises(ValueError, match="no answer"):
        catcher.wait(open_browser=lambda url: None, timeout=0.1)


def test_the_code_is_converted_into_the_apps_client_and_slug() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "id": 1,
                "slug": "adk-voice-agent-123",
                "client_id": "Iv1.abc",
                "client_secret": "s3cret",
                "pem": "-----BEGIN RSA PRIVATE KEY-----",
            },
        )

    set_default_http_transport(httpx.MockTransport(handler))
    env = github_app.app_env(github_app.convert("the-code"))

    assert env == {
        "GITHUB_CLIENT_ID": "Iv1.abc",
        "GITHUB_CLIENT_SECRET": "s3cret",
        "GITHUB_APP_SLUG": "adk-voice-agent-123",
    }
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == (
        "https://api.github.com/app-manifests/the-code/conversions"
    )


def test_a_failed_conversion_says_why() -> None:
    set_default_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(404, text="Not Found")
        )
    )
    with pytest.raises(ValueError, match="404"):
        github_app.convert("expired")
    with pytest.raises(ValueError, match="client_secret"):
        github_app.app_env({"slug": "s", "client_id": "c"})
