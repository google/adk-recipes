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

"""Ship the app to the Cloud Run service terraform/ provisions (`make deploy`, after `terraform apply`)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

from dotenv import dotenv_values, set_key

from app.workers.providers.gce import computer_filter

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_DIR = ROOT / "terraform"
# Local-only or service-derived keys excluded from `.env` staging.
LOCAL_ONLY_KEYS = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "THREAD_DB_URL",
        "APP_URL",
        "VOICE_AGENT_AUTH_MODE",
        "VOICE_AGENT_IAP_AUDIENCE",
        "VOICE_AGENT_DEV_USER_ID",
        "GOOGLE_CLOUD_AGENT_ENGINE_ID",
        "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION",
    }
)
LOCAL_WORKSPACES = "config/workspaces.local.yaml"


def fail(message: str) -> NoReturn:
    sys.exit(f"❌ {message}")


def env_example() -> dict[str, str | None]:
    return dotenv_values(ROOT / ".env.example")


def _is_placeholder(value: str | None) -> bool:
    return (
        not value
        or value == "your-gcp-project-id"
        or (value.startswith("<TODO") and value.endswith(">"))
    )


def declared_env(env: dict[str, str | None]) -> dict[str, str]:
    """Filter `.env` to non-blank keys declared in `.env.example` so stale keys or tokens are never shipped."""
    declared = env_example().keys()
    present = {
        key: value
        for key, value in env.items()
        if value and not _is_placeholder(value)
    }
    if skipped := sorted(present.keys() - declared):
        print(
            f"Not deployed (not declared in .env.example): {', '.join(skipped)}",
            flush=True,
        )
    return {key: value for key, value in present.items() if key in declared}


def resolve_project() -> str | None:
    placeholder = env_example().get("GOOGLE_CLOUD_PROJECT")
    for project in (
        os.getenv("GOOGLE_CLOUD_PROJECT"),
        dotenv_values(ROOT / ".env").get("GOOGLE_CLOUD_PROJECT"),
    ):
        if project and project != placeholder and not _is_placeholder(project):
            return project
    if not shutil.which("gcloud"):
        return None
    gcloud = ["gcloud", "config", "get-value", "project"]
    out = subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
        gcloud, capture_output=True, text=True, check=False
    ).stdout
    return out.strip() or None


def terraform_vars(
    project: str,
    region: str,
    var_file: str | None = None,
    computer_image: str | None = None,
) -> list[str]:
    return [
        f"-var=project_id={project}",
        f"-var=region={region}",
        *([f"-var-file={var_file}"] if var_file else []),
        *([f"-var=computer_image={computer_image}"] if computer_image else []),
    ]


def terraform_console(
    expression: str, project: str, region: str, var_file: str | None = None
):
    """Evaluate `expression` via `terraform console` (works before first apply and honors `TF_WORKSPACE`)."""
    result = subprocess.run(  # noqa: S603 - fixed terraform argv, no shell
        [  # noqa: S607 - terraform resolved from PATH
            "terraform",
            f"-chdir={TERRAFORM_DIR}",
            "console",
            *terraform_vars(project, region, var_file),
        ],
        input=f"jsonencode({expression})",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        fail(
            f"terraform console failed (run `terraform -chdir=terraform init`):\n{result.stderr}"
        )
    # `terraform console` prints the JSON document as a quoted HCL string.
    return json.loads(json.loads(result.stdout))


def deploy_config(
    project: str, region: str, var_file: str | None = None
) -> dict:
    """Return `local.deploy` from applied state if it matches `project`, else via `terraform console`."""
    applied = subprocess.run(  # noqa: S603 - fixed terraform argv, no shell
        ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json", "deploy"],  # noqa: S607 - terraform resolved from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode == 0:
        with contextlib.suppress(ValueError):
            config = json.loads(applied.stdout)
            # Ignore state left over from a different project.
            if isinstance(config, dict) and str(
                config.get("service_account_email", "")
            ).endswith(f"@{project}.iam.gserviceaccount.com"):
                return config
    return terraform_console("local.deploy", project, region, var_file)


def computer_image(
    project: str,
    region: str,
    var_file: str | None = None,
    requested: str | None = None,
) -> str:
    """Resolve the computer image (`requested`, tfvars pin, or applied non-`:latest` image)."""
    if requested:
        return requested
    if pinned := terraform_console(
        "var.computer_image", project, region, var_file
    ):
        return str(pinned)
    applied = terraform_output("computer_image")
    if applied and not applied.endswith(":latest"):
        return applied
    fail(
        "no computer image to deploy. Build one with `make computer-image`, then run "
        "`make deploy COMPUTER_IMAGE=<the image it prints>` (or pin computer_image in "
        "your tfvars). `make onboard` does both."
    )


def project_number(project: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
        [  # noqa: S607 - gcloud resolved from PATH
            "gcloud",
            "projects",
            "describe",
            project,
            "--format=value(projectNumber)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    number = result.stdout.strip()
    if result.returncode or not number.isdigit():
        fail(
            f"could not read the project number of {project}:\n{result.stderr}"
        )
    return number


def iap_audience(number: str, region: str, service_name: str) -> str:
    return f"/projects/{number}/locations/{region}/services/{service_name}"


def service_url(number: str, region: str, service_name: str) -> str:
    return f"https://{service_name}-{number}.{region}.run.app"


def gcloud_account() -> str:
    result = subprocess.run(
        ["gcloud", "config", "get-value", "account"],  # noqa: S607 - gcloud resolved from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def terraform_output(name: str) -> str | None:
    result = subprocess.run(  # noqa: S603 - fixed terraform argv, no shell
        ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-raw", name],  # noqa: S607 - terraform resolved from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return (
        value
        if result.returncode == 0 and value and "\n" not in value
        else None
    )


def computer_mcp_audience(app_url: str) -> str:
    """Cloud Run IAP uses a Google-managed OAuth client, which requires a `signJwt` audience of `<app_url>/*`."""
    return f"{app_url}/*"


def memory_engine(*, dry_run: bool) -> str:
    engine = terraform_output("agent_engine")
    if not engine:
        if not dry_run:
            fail("no agent_engine output: run `terraform apply` first.")
        engine = "<agent engine>"
    return engine


def register_memory_schema(engine: str) -> None:
    """Register the Memory Bank profile schema with deployer credentials (the runtime SA lacks engine-update permission)."""
    from app.memory.store import ensure_profile_schema

    os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = engine
    print(f"Memory Bank profile schema: {ensure_profile_schema()}", flush=True)


def computer_env(
    config: dict, app_url: str, *, dry_run: bool
) -> dict[str, str]:
    template = terraform_output("computer_instance_template")
    if not template:
        if not dry_run:
            fail(
                "no computer_instance_template output: run `terraform apply` first."
            )
        template = "<computer instance template>"
    return {
        "COMPUTER_PROVIDER": "gce",
        "COMPUTER_INSTANCE_TEMPLATE": template,
        "COMPUTER_ZONE": config["computer_zone"],
        "COMPUTER_SERVICE_ACCOUNT": config["computer_service_account_email"],
        "COMPUTER_SNAPSHOT_POLICY": config["computer_snapshot_policy"],
        "COMPUTER_DEPLOYMENT": config["computer_deployment"],
        "COMPUTER_MCP_AUDIENCE": computer_mcp_audience(app_url),
        "APP_URL": app_url,
    }


def delete_computers(project: str, config: dict, *, purge: bool) -> None:
    """Delete this deployment's runtime-created VMs (and disks when `purge=True`) before `terraform destroy`."""

    def names(kind: str, filter_: str) -> list[str]:
        listing = subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
            [  # noqa: S607 - gcloud resolved from PATH
                "gcloud", "compute", kind, "list", f"--project={project}",
                f"--filter={filter_}", "--format=value(name)",
            ],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return listing.stdout.split()

    zone = config["computer_zone"]
    scope = f"{computer_filter(config['computer_deployment'])} AND zone:{zone}"
    network = f"/networks/{config['computer_network']}"
    kinds = [("instances", f"{scope} AND networkInterfaces.network~{network}$")]
    if purge:
        kinds.append(("disks", scope))
    for kind, filter_ in kinds:
        found = names(kind, filter_)
        if not found:
            continue
        print(f"Deleting {kind}: {', '.join(found)}", flush=True)
        subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
            [  # noqa: S607 - gcloud resolved from PATH
                "gcloud", "compute", kind, "delete", *found,
                f"--project={project}", f"--zone={zone}", "--quiet",
            ],
            check=True,
        )  # fmt: skip
    if not purge:
        policy = config.get("computer_snapshot_policy")
        for disk in names("disks", scope) if policy else []:
            subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
                [  # noqa: S607 - gcloud resolved from PATH
                    "gcloud", "compute", "disks", "remove-resource-policies",
                    disk, f"--resource-policies={policy}",
                    f"--project={project}", f"--zone={zone}", "--quiet",
                ],
                check=False,
            )  # fmt: skip
        print(
            "Home disks kept without scheduled snapshots until the next `make deploy` "
            "(PURGE=1 deletes them)."
        )


def _gcloud_secret(project: str, *args: str, value: str | None = None):
    return subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
        ["gcloud", "secrets", *args, f"--project={project}", "--quiet"],  # noqa: S607 - gcloud resolved from PATH
        input=value,
        capture_output=True,
        text=True,
        check=False,
    )


def sync_credentials(
    project: str, secrets: dict[str, str], env: dict[str, str], *, dry_run: bool
) -> dict[str, str]:
    """Copy changed `.env` values into Secret Manager; return the env -> secret pairs that have a value."""
    mounted: dict[str, str] = {}
    for key, secret in sorted(secrets.items()):
        latest = _gcloud_secret(
            project, "versions", "access", "latest", f"--secret={secret}"
        )
        current = latest.stdout if latest.returncode == 0 else None
        value = env.get(key)
        if value and value != current:
            if not dry_run:
                added = _gcloud_secret(
                    project,
                    "versions",
                    "add",
                    secret,
                    "--data-file=-",
                    value=value,
                )
                if added.returncode:
                    fail(f"could not store {key} in {secret}:\n{added.stderr}")
            print(
                f"{'Would store' if dry_run else '✓ Stored'} {key} in {secret}",
                flush=True,
            )
            current = value
        if current is not None:
            mounted[key] = secret
    return mounted


def sync_instance_template(
    project: str,
    region: str,
    service_name: str,
    template: str,
    *,
    dry_run: bool,
) -> None:
    """Update Cloud Run's `COMPUTER_INSTANCE_TEMPLATE` before `agents-cli deploy` builds a new image."""
    if dry_run or not template:
        return
    subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
        [  # noqa: S607 - gcloud resolved from PATH
            "gcloud",
            "run",
            "services",
            "update",
            service_name,
            f"--project={project}",
            f"--region={region}",
            f"--update-env-vars=COMPUTER_INSTANCE_TEMPLATE={template}",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def stage_repo(dest: Path, env: dict[str, str]) -> None:
    """Stage tracked/unignored files plus `config/workspaces.local.yaml` and a filtered `.env` for `agents-cli deploy`."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607 - git resolved from PATH
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    for rel in [*filter(None, listing.split("\0")), LOCAL_WORKSPACES]:
        src = ROOT / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    env_file = dest / ".env"
    env_file.touch()
    for key, value in env.items():
        set_key(env_file, key, value)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project", help="GCP project (default: see --print-project)"
    )
    parser.add_argument("--region", default="us-central1")
    parser.add_argument(
        "--var-file",
        help="terraform tfvars file of this deployment (`make deploy TFVARS=...`)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="change nothing; print the deploy",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="never prompt on stdin (accepted for parity with onboard.py)",
    )
    parser.add_argument("--print-project", action="store_true")
    parser.add_argument(
        "--print-computer-image",
        action="store_true",
        help="print the computer image to roll out and exit (`make deploy`)",
    )
    parser.add_argument(
        "--computer-image",
        help="with --print-computer-image: the image asked for (`COMPUTER_IMAGE=`)",
    )
    parser.add_argument(
        "--delete-computers",
        action="store_true",
        help="delete this deployment's computers and exit (`make destroy`)",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="with --delete-computers, delete the users' home disks too",
    )
    args = parser.parse_args(argv)

    project = args.project or resolve_project()
    if args.print_project:
        if not project:
            fail(
                "no GCP project: set GOOGLE_CLOUD_PROJECT in .env or run `gcloud config set project <id>`."
            )
        print(project)
        return
    if args.print_computer_image:
        if not project:
            fail("no GCP project: pass --project.")
        print(
            computer_image(
                project, args.region, args.var_file, args.computer_image
            )
        )
        return
    for tool in ("terraform", "gcloud", "git", "agents-cli"):
        if not shutil.which(tool):
            fail(f"`{tool}` is not on PATH (agents-cli: run `uv sync`).")
    if not project:
        fail(
            "no GCP project: pass --project, set GOOGLE_CLOUD_PROJECT in .env, or run `gcloud config set project <id>`."
        )

    config = deploy_config(project, args.region, args.var_file)
    if args.delete_computers:
        delete_computers(project, config, purge=args.purge)
        return
    env = declared_env(dotenv_values(ROOT / ".env"))
    secrets = config["managed_secrets"] | sync_credentials(
        project, config["credential_secrets"], env, dry_run=args.dry_run
    )
    excluded = (
        LOCAL_ONLY_KEYS | set(config["credential_env_keys"]) | secrets.keys()
    )
    service_account = config["service_account_email"]
    number = project_number(project)
    audience = iap_audience(number, args.region, config["service_name"])
    app_url = service_url(number, args.region, config["service_name"])
    engine = memory_engine(dry_run=args.dry_run)
    if not args.dry_run:
        register_memory_schema(engine)
    runtime_env = {
        "GOOGLE_CLOUD_PROJECT": project,
        "GOOGLE_CLOUD_AGENT_ENGINE_ID": engine,
        "SANDBOX_CALLER_SA": service_account,
        **computer_env(config, app_url, dry_run=args.dry_run),
        "VOICE_AGENT_AUTH_MODE": "iap",
        "VOICE_AGENT_IAP_AUDIENCE": audience,
    }
    sync_instance_template(
        project,
        args.region,
        config["service_name"],
        runtime_env["COMPUTER_INSTANCE_TEMPLATE"],
        dry_run=args.dry_run,
    )

    cmd = [
        "agents-cli",
        "deploy",
        f"--project={project}",
        f"--region={args.region}",
        f"--service-name={config['service_name']}",
        f"--service-account={service_account}",
        "--iap",
        # Match cloud_run.tf (single warm instance so in-process thread/session state isn't split).
        "--min-instances=1",
        "--max-instances=1",
        "--concurrency=40",
        "--cpu=1",
        "--memory=2Gi",
        "--update-env-vars="
        + ",".join(f"{k}={v}" for k, v in runtime_env.items()),
        "--no-confirm-project",
    ]
    if secrets:
        cmd.append(
            "--secrets="
            + ",".join(f"{k}={v}" for k, v in sorted(secrets.items()))
        )
    if args.dry_run:
        cmd.append("--dry-run")

    with tempfile.TemporaryDirectory(
        prefix="adk-voice-agent-deploy-"
    ) as staging:
        deployed_env = {k: v for k, v in env.items() if k not in excluded}
        stage_repo(Path(staging), deployed_env)
        code = subprocess.run(cmd, cwd=staging, check=False).returncode  # noqa: S603 - fixed agents-cli argv, no shell
    if code == 0:
        print(f"\nService URL: {app_url}")
    sys.exit(code)


if __name__ == "__main__":
    main()
