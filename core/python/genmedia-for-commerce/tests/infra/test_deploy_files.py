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

"""The deploy files keep their least-privilege settings: the Makefile,
the Terraform IAM and the Dockerfile, read as text."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TERRAFORM = ROOT / "infra" / "terraform"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _account_ids() -> set[str]:
    return set(
        re.findall(r'account_id\s*=\s*"([^"]+)"', _read(TERRAFORM / "iam.tf"))
    )


def test_make_deploy_uses_the_terraform_service_accounts():
    makefile = _read(ROOT / "Makefile")
    assert {"genmedia-cloudrun", "genmedia-build"} <= _account_ids()
    assert (
        "--service-account=genmedia-cloudrun@$$PROJECT_ID.iam.gserviceaccount.com"
        in makefile
    )
    assert (
        "--build-service-account=projects/$$PROJECT_ID/serviceAccounts/"
        "genmedia-build@$$PROJECT_ID.iam.gserviceaccount.com" in makefile
    )


def test_no_project_wide_storage_or_act_as_roles():
    terraform = "".join(_read(f) for f in TERRAFORM.glob("*.tf"))
    project_roles = re.findall(
        r'resource "google_project_iam_member".*?\n}', terraform, re.DOTALL
    )
    assert project_roles
    for block in project_roles:
        if "roles/storage." in block:
            # Only when a condition limits it to the media bucket.
            assert "condition {" in block
            assert (
                "projects/_/buckets/${google_storage_bucket.media.name}"
                in block
            )
        assert "roles/iam.serviceAccountUser" not in block
        assert "-compute@developer.gserviceaccount.com" not in block


def test_every_bucket_prevents_public_access():
    storage = _read(TERRAFORM / "storage.tf")
    buckets = storage.count('resource "google_storage_bucket" ')
    assert buckets
    enforced = re.findall(r'public_access_prevention\s*=\s*"enforced"', storage)
    assert len(enforced) == buckets


def test_the_image_runs_as_an_unprivileged_user():
    dockerfile = _read(ROOT / "Dockerfile")
    users = re.findall(r"^USER (\S+)", dockerfile, re.MULTILINE)
    assert users[-1] == "10001"
    assert "/root" not in dockerfile


def test_setup_infra_retries_terraform_apply():
    # A fresh project's new APIs take a minute to be usable (403).
    makefile = _read(ROOT / "Makefile")
    recipe = makefile.split("\nsetup-infra:", 1)[1].split("\n\n", 1)[0]
    assert "for attempt in 1 2 3" in recipe
    assert "apply && exit 0" in recipe
    assert "-auto-approve" not in recipe


def test_the_makefile_has_no_jinja_markers():
    # agent-starter-pack renders the Makefile with Jinja2.
    makefile = _read(ROOT / "Makefile")
    for marker in ("{{", "{%", "{#"):
        assert marker not in makefile, marker


# ----------------------------------------------- genmedia-web (make web)


def _block(terraform: str, header: str) -> str:
    return terraform.split(header, 1)[1].split("\n}\n", 1)[0]


def test_terraform_creates_genmedia_web_for_the_deployer_only():
    iam = _read(TERRAFORM / "iam.tf")
    assert "genmedia-web" in _account_ids()
    web = _block(iam, 'resource "google_service_account" "web"')
    assert "invokes the Cloud Run service only" in web
    # Token Creator on that account only, for whoever runs Terraform
    grant = _block(
        iam, 'resource "google_service_account_iam_member" "web_deployer"'
    )
    assert "google_service_account.web.name" in grant
    assert (
        'role               = "roles/iam.serviceAccountTokenCreator"' in grant
    )
    assert "local.deployer_member" in grant
    assert 'data "google_client_openid_userinfo" "deployer" {}' in iam
    assert '? "serviceAccount:${local.deployer_email}"' in iam
    assert ': "user:${local.deployer_email}"' in iam
    # Nothing project-wide: the invoker role is on the service (make deploy)
    terraform = "".join(_read(f) for f in TERRAFORM.glob("*.tf"))
    project_roles = re.findall(
        r'resource "google_project_iam_member".*?\n}', terraform, re.DOTALL
    )
    assert not [b for b in project_roles if "google_service_account.web" in b]
    assert '"roles/run.invoker"' not in terraform
    outputs = _read(TERRAFORM / "outputs.tf")
    assert "value       = google_service_account.web.email" in outputs


def _make(cwd: Path, *args: str, **env: str):
    """The Makefile run from a folder with an empty config.env (no recipe
    makes one) and no gcloud on PATH unless a test adds a fake."""
    (cwd / "config.env").touch()
    return subprocess.run(
        ["make", "-f", str(ROOT / "Makefile"), *args],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"} | env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_make_deploy_lets_genmedia_web_invoke_the_service(tmp_path):
    recipe = _make(tmp_path, "-n", "deploy").stdout

    assert (
        "gcloud run services add-iam-policy-binding "
        "${SERVICE_NAME:-genmedia-for-commerce} "
        "--region=${DEFAULT_REGION:-us-central1} --project=$PROJECT_ID "
        "--member=serviceAccount:genmedia-web@$PROJECT_ID.iam.gserviceaccount.com "
        "--role=roles/run.invoker --quiet" in recipe
    )
    # After the deploy succeeded, before the web app proxy starts
    assert recipe.index("run.invoker") > recipe.index("gcloud run deploy")
    assert recipe.index("run.invoker") < recipe.index(
        "web_proxy.sh --background"
    )


# Records its arguments; add-iam-policy-binding answers as FAKE_BINDING says
BINDING_GCLOUD = """#!/bin/bash
echo "$*" >> "$FAKE_DIR/calls"
case "$FAKE_BINDING" in
missing) echo "ERROR: INVALID_ARGUMENT: Service account genmedia-web@my-proj.iam.gserviceaccount.com does not exist." >&2; exit 1 ;;
denied) echo "ERROR: PERMISSION_DENIED: run.services.setIamPolicy" >&2; exit 1 ;;
esac
echo "bindings: []"
"""

GRANT_TARGET = """
grant-test:
\t@PROJECT_ID=my-proj; SERVICE_NAME=my-svc; DEFAULT_REGION=europe-west1; \\
\t$(GRANT_WEB_INVOKER); echo "deploy goes on"
"""


@pytest.fixture
def grant(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(BINDING_GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    (tmp_path / "grant.mk").write_text(GRANT_TARGET)

    def run(binding: str):
        result = _make(
            tmp_path,
            "-f",
            "grant.mk",
            "grant-test",
            PATH=f"{bin_dir}:/usr/bin:/bin",
            FAKE_DIR=str(tmp_path),
            FAKE_BINDING=binding,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.endswith("deploy goes on\n")
        return result.stdout, (tmp_path / "calls").read_text()

    return run


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_the_invoker_grant_names_the_account_and_service(grant):
    out, calls = grant("ok")

    assert calls == (
        "run services add-iam-policy-binding my-svc --region=europe-west1 "
        "--project=my-proj "
        "--member=serviceAccount:genmedia-web@my-proj.iam.gserviceaccount.com "
        "--role=roles/run.invoker --quiet\n"
    )
    assert "can invoke the service" in out


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_an_older_setup_is_told_to_run_setup_infra(grant):
    out, _ = grant("missing")

    assert "run make setup-infra, then make deploy" in out


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_a_refused_grant_is_a_warning_only(grant):
    out, _ = grant("denied")

    assert "Warning: could not let genmedia-web" in out
    assert "PERMISSION_DENIED" in out


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_make_share_grants_token_creator_and_service_viewer(tmp_path):
    usage = _make(tmp_path, "share")
    users = "USERS=user:a@x.com,group:g@x.com"
    recipe = _make(tmp_path, "-n", "share", users).stdout

    assert usage.returncode != 0
    assert 'make share USERS="user:a@example.com' in usage.stdout
    assert (
        "gcloud iam service-accounts add-iam-policy-binding "
        "genmedia-web@$PROJECT_ID.iam.gserviceaccount.com "
        '--project=$PROJECT_ID --member="$member" '
        "--role=roles/iam.serviceAccountTokenCreator --quiet" in recipe
    )
    assert (
        "gcloud run services add-iam-policy-binding "
        "${SERVICE_NAME:-genmedia-for-commerce} "
        "--region=${DEFAULT_REGION:-us-central1} --project=$PROJECT_ID "
        '--member="$member" --role=roles/run.viewer --quiet' in recipe
    )
    assert "echo \"user:a@x.com,group:g@x.com\" | tr ',' ' '" in recipe


# uv: the registration script exits with FAKE_STATUS; CONFIG_SH's config
FAKE_UV = """#!/bin/bash
case "$*" in
*infra/gemini_enterprise.py*) echo "script: ${*: -1}"; exit "$FAKE_STATUS" ;;
*) echo "PROJECT_ID=my-proj" ;;
esac
"""


@pytest.fixture
def register(tmp_path):
    """make register-gemini-enterprise with a fake uv and web_proxy.sh."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(FAKE_UV)
    (bin_dir / "uv").chmod(0o755)
    (tmp_path / "infra").mkdir()
    # It fails too: that must not change the registration's status
    (tmp_path / "infra" / "web_proxy.sh").write_text(
        'echo "proxy $* for $PROJECT_ID"; exit 1\n'
    )

    def run(status: int, *args: str):
        return _make(
            tmp_path,
            "register-gemini-enterprise",
            *args,
            PATH=f"{bin_dir}:/usr/bin:/bin",
            FAKE_STATUS=str(status),
        )

    return run


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
@pytest.mark.parametrize("status", [0, 1])
def test_registration_starts_the_proxy_and_keeps_its_status(register, status):
    result = register(status)

    assert "proxy --background for my-proj" in result.stdout
    assert (result.returncode == 0) is (status == 0)


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_a_dry_run_registration_starts_no_proxy(register):
    result = register(0, "DRY_RUN=1")

    assert result.returncode == 0
    assert "script: --dry-run" in result.stdout
    assert "proxy" not in result.stdout


@pytest.mark.skipif(not shutil.which("make"), reason="no make")
def test_make_allow_mcp_connectors_runs_the_override(tmp_path):
    recipe = _make(tmp_path, "-n", "allow-mcp-connectors").stdout

    assert recipe == (
        "uv run python infra/gemini_enterprise.py --allow-mcp-connectors\n"
    )
