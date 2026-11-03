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

"""Set up a deployment (`make onboard`): asks for the Google Cloud project, then does the rest."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from app.app_utils.env_flags import env_flag
from app.auth.env_store import dotenv_path, persist_env_vars, read_dotenv
from app.auth.github.api import GITHUB_PROVIDER
from app.auth.github.provider import GITHUB_APP_SLUG_ENV
from app.auth.providers import WORKSPACE_PROVIDER, require_provider
from scripts import deploy
from scripts.provisioning import ui
from scripts.provisioning.common import (
    ENV_EXAMPLE_FILE,
    PROJECT_ROOT,
    redirect_uris,
    run_quiet,
)
from scripts.provisioning.github_app import create_github_app
from scripts.provisioning.sandbox_image import (
    IMAGE_NAME,
    default_tag,
    ensure_image,
)

TOOLS = ("terraform", "gcloud", "git", "agents-cli")
GOOGLE_CONSOLE = (
    "https://console.cloud.google.com/apis/credentials?project={project}"
)
GOOGLE_CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
APP_DISPLAY_NAME = "ADK Voice Agent"


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    code = subprocess.run(cmd, check=False).returncode  # noqa: S603 - fixed argv, no shell
    if code:
        deploy.fail(
            f"`{cmd[0]}` exited with {code}; fix the error above and rerun `make onboard`."
        )


def ask_project(default: str | None, *, interactive: bool) -> str:
    project = (
        ui.ask("Google Cloud project", default or "")
        if interactive
        else default
    )
    if not project:
        deploy.fail(
            "no GCP project: pass --project (or PROJECT=<id>), set "
            "GOOGLE_CLOUD_PROJECT in .env, or run `gcloud config set project <id>`."
        )
    return project


def save_project(project: str) -> None:
    env_file = dotenv_path()
    if not env_file.exists() and ENV_EXAMPLE_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE_FILE, env_file)
        ui.ok(f"Created {env_file.name} from {ENV_EXAMPLE_FILE.name}")
    persist_env_vars({"GOOGLE_CLOUD_PROJECT": project})
    ui.ok(f"GOOGLE_CLOUD_PROJECT={project} saved to {env_file.name}")


def current_env() -> dict[str, str]:
    return {
        k: v for k, v in {**read_dotenv(), **os.environ}.items() if v.strip()
    }


def _ask_google_client_id() -> str:
    for _ in range(3):
        client_id = ui.ask("Client ID (Enter to skip Google)")
        if not client_id or client_id.endswith(GOOGLE_CLIENT_ID_SUFFIX):
            return client_id
        ui.warn(f"A client ID ends with {GOOGLE_CLIENT_ID_SUFFIX}; try again.")
    return ""


def register_google(
    project: str, app_url: str | None, env: dict[str, str], *, interactive: bool
) -> dict[str, str]:
    google = require_provider(WORKSPACE_PROVIDER)
    keys = (google.client_id_env, google.client_secret_env)
    uris = redirect_uris(WORKSPACE_PROVIDER, app_url)
    ui.section("📅", "Google: Calendar, Gmail, Drive, Docs, Sheets, Slides")
    if all(env.get(k) for k in keys):
        ui.ok(
            f"OAuth client already set ({google.client_id_env}). It must belong to "
            f"{project}, where terraform enables the Google APIs, and list these "
            "redirect URIs:"
        )
        for uri in uris:
            ui.info(f"   {uri}")
        return {k: env[k] for k in keys}
    if not interactive:
        ui.skipped(f"Skipped: set {keys[0]} and {keys[1]} to register it.")
        return {}
    ui.info(
        "Google has no API for OAuth clients, so this is a two-minute copy-paste.\n"
        f"{ui.INDENT}Create it in this project, {project}: terraform enables the Google "
        "APIs it calls here and nowhere else."
    )
    ui.action(
        1,
        "Open the Credentials page:",
        GOOGLE_CONSOLE.format(project=project),
    )
    ui.action(
        2,
        "First time only: the console asks you to configure the consent screen.",
        f"App name: {APP_DISPLAY_NAME}",
        f"Support and contact email: {deploy.gcloud_account() or 'your email'}",
        "Audience: Internal if your account is in a Workspace org,",
        "  else External, then add yourself under Test users",
    )
    ui.action(
        3,
        "Create credentials → OAuth client ID",
        "Application type: Web application",
        f"Name: {APP_DISPLAY_NAME}",
    )
    ui.action(4, "Under Authorized redirect URIs, add each:", *uris)
    ui.action(5, "Click Create, then paste the client ID and secret here.")
    client_id = _ask_google_client_id()
    if not client_id:
        ui.skipped("Google skipped. Rerun `make onboard` to add it later.")
        return {}
    client_secret = ui.ask_secret("Client secret")
    if not client_secret:
        ui.skipped("Google skipped (no client secret).")
        return {}
    ui.ok(f"Google OAuth client received ({client_id[:12]}…)")
    return {keys[0]: client_id, keys[1]: client_secret}


def register_github(
    app_url: str | None,
    env: dict[str, str],
    *,
    interactive: bool,
    org: str | None = None,
) -> dict[str, str]:
    github = require_provider(GITHUB_PROVIDER)
    keys = (github.client_id_env, github.client_secret_env, GITHUB_APP_SLUG_ENV)
    ui.section("🐙", "GitHub: repos, pull requests, issues")
    if all(env.get(k) for k in keys):
        ui.ok(
            f"App already set ({GITHUB_APP_SLUG_ENV}={env[GITHUB_APP_SLUG_ENV]}). Its "
            "callback URLs (the App's settings on GitHub) must include:"
        )
        for uri in redirect_uris(GITHUB_PROVIDER, app_url):
            ui.info(f"   {uri}")
        return {k: env[k] for k in keys}
    if not interactive:
        ui.skipped(f"Skipped: set {', '.join(keys)} to register it.")
        return {}
    where = f"the {org} organization" if org else "your GitHub account"
    ui.info(
        "No copy-paste: your browser opens GitHub with the App filled in; you click "
        "Create."
    )
    if not org:
        ui.info(
            "To create it under an organization instead, rerun with GITHUB_ORG=<org>."
        )
    if not ui.confirm(f"Create this deployment's GitHub App under {where}?"):
        ui.skipped("GitHub skipped. Rerun `make onboard` to add it later.")
        return {}
    try:
        created = create_github_app(app_url, org=org)
    except ValueError as exc:
        ui.error(f"GitHub: {exc} Rerun `make onboard` to try again.")
        return {}
    ui.ok(f"GitHub App {created[GITHUB_APP_SLUG_ENV]} created")
    return created


def register_apps(
    project: str,
    app_url: str | None,
    *,
    interactive: bool,
    skip_google: bool = False,
    skip_github: bool = False,
    github_org: str | None = None,
) -> None:
    ui.section(
        "🔌",
        "Connected apps",
        "Each is optional; users connect their own accounts in Settings → Apps. "
        "Rerun `make onboard` to add one later.",
    )
    env = current_env()
    updates: dict[str, str] = {}
    if skip_google:
        ui.skipped("Google skipped (--skip-google).")
    else:
        updates |= register_google(
            project, app_url, env, interactive=interactive
        )
    if skip_github:
        ui.skipped("GitHub skipped (--skip-github).")
    else:
        updates |= register_github(
            app_url, env, interactive=interactive, org=github_org
        )
    # Persist shell-supplied credentials to `.env` so `scripts/deploy.py` syncs them to Secret Manager.
    saved = read_dotenv()
    if changed := {k: v for k, v in updates.items() if saved.get(k) != v}:
        persist_env_vars(dict(changed))
        ui.ok(f"Saved {', '.join(changed)} to {dotenv_path().name}")


def next_steps(app_url: str) -> str:
    return f"""
🎉 Done. Your deployment is ready.
{ui.INDENT}👉 1. Open {app_url} and sign in.
{ui.INDENT}      To let others in, add them to iap_users in terraform/terraform.tfvars
{ui.INDENT}      and run `make deploy`.
{ui.INDENT}👉 2. Connect your apps in Settings → Apps.
{ui.INDENT}👉 3. Tap Live and talk. Threads run on your own computer: `make ssh` opens a
{ui.INDENT}      shell there, and `make doctor` checks it.
"""


def deployed_app_url(project: str, region: str, var_file: str | None) -> str:
    config = deploy.deploy_config(project, region, var_file)
    number = deploy.project_number(project)
    return deploy.service_url(number, region, config["service_name"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="default: asked for (see above)")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument(
        "--var-file", help="terraform tfvars file of this deployment"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="terraform plan and a deploy preview; build and change nothing",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        default=ui.non_interactive(),
        help="never prompt on stdin (default: NONINTERACTIVE=1 or CI=1)",
    )
    parser.add_argument(
        "--skip-google",
        action="store_true",
        default=env_flag("SKIP_GOOGLE"),
        help="don't register the Google app (default: SKIP_GOOGLE)",
    )
    parser.add_argument(
        "--skip-github",
        action="store_true",
        default=env_flag("SKIP_GITHUB"),
        help="don't create the GitHub App (default: SKIP_GITHUB)",
    )
    parser.add_argument(
        "--github-org",
        default=os.getenv("GITHUB_ORG") or None,
        help="create the GitHub App under this organization (default: GITHUB_ORG, "
        "else your account)",
    )
    args = parser.parse_args(argv)

    no_input = args.no_input or ui.non_interactive()
    interactive = sys.stdin.isatty() and not no_input
    print(
        "🚀 Set up ADK Voice Agent"
        + (
            " (dry run: registers, builds, and saves nothing)"
            if args.dry_run
            else ""
        )
        + f"\n{ui.INDENT}Questions first, then about 15 unattended minutes: terraform, "
        "the computer image, and the deploy.",
        flush=True,
    )
    if missing := [t for t in TOOLS if not shutil.which(t)]:
        deploy.fail(
            f"not on PATH: {', '.join(missing)} (agents-cli: run `uv sync`)."
        )
    ui.section("☁️ ", "Google Cloud")
    project = args.project or ask_project(
        deploy.resolve_project(), interactive=interactive
    )
    if args.dry_run:
        ui.info(
            f"Would save GOOGLE_CLOUD_PROJECT={project} to {dotenv_path().name}"
        )
    else:
        save_project(project)
    if not run_quiet(
        ["gcloud", "auth", "application-default", "print-access-token"],
        timeout=60,
    ):
        deploy.fail(
            "terraform needs Application Default Credentials: run "
            "`gcloud auth application-default login`, then `make onboard` again."
        )
    ui.ok("Application Default Credentials found")

    terraform = ["terraform", f"-chdir={deploy.TERRAFORM_DIR}"]
    run([*terraform, "init", "-input=false"])
    app_url = deployed_app_url(project, args.region, args.var_file)
    if args.dry_run:
        ui.section("🔌", "Connected apps")
        ui.skipped("Not registered in a dry run.")
    else:
        register_apps(
            project,
            app_url,
            interactive=interactive,
            skip_google=args.skip_google,
            skip_github=args.skip_github,
            github_org=args.github_org,
        )
    repository = deploy.terraform_console(
        "local.computer_repo_url", project, args.region, args.var_file
    )
    tag = default_tag()
    tf_vars = deploy.terraform_vars(
        project,
        args.region,
        args.var_file,
        f"{repository}/{IMAGE_NAME}:{tag}",
    )
    ui.section(
        "🏗️ ",
        "Infrastructure (terraform)",
        "No more questions from here; you can leave this running.",
    )
    if args.dry_run:
        run([*terraform, "plan", "-input=false", *tf_vars])
    else:
        run([*terraform, "apply", "-input=false", "-auto-approve", *tf_vars])
        persist_env_vars(
            {
                "GOOGLE_CLOUD_AGENT_ENGINE_ID": deploy.memory_engine(
                    dry_run=False
                )
            }
        )
        ui.section("📦", "The computer image")
        ensure_image(project, args.region, tag, repository)

    ui.section("🚀", "The app (Cloud Run behind IAP)")
    run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "deploy.py"),
            f"--project={project}",
            f"--region={args.region}",
            *([f"--var-file={args.var_file}"] if args.var_file else []),
            *(["--dry-run"] if args.dry_run else []),
        ]
    )
    if not args.dry_run:
        print(next_steps(app_url), flush=True)


if __name__ == "__main__":
    main()
