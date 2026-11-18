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

"""The computer image (`sandbox/Dockerfile`): build it and push it to Artifact Registry."""

from __future__ import annotations

import argparse
import os
import subprocess

from app.auth.env_store import load_dotenv_defaults
from app.workers.factory import sandbox_project_and_location
from scripts.provisioning import ui
from scripts.provisioning.common import PROJECT_ROOT, gcloud, run_quiet

REPOSITORY = "adk-voice-agent"
IMAGE_NAME = "computer"
_SANDBOX_DIR = PROJECT_ROOT / "sandbox"
# Builds one multi-arch tag (amd64 for GCE, arm64 for docker on Apple Silicon).
_CLOUDBUILD = _SANDBOX_DIR / "cloudbuild.yaml"
_TERRAFORM_DIR = PROJECT_ROOT / "terraform"


def repository_url(project: str, region: str) -> str:
    applied = run_quiet(
        [
            "terraform",
            f"-chdir={_TERRAFORM_DIR}",
            "output",
            "-raw",
            "computer_image_repository",
        ],
        timeout=60,
    )
    # Before the first apply terraform prints a warning instead of a value.
    if (
        applied
        and ".pkg.dev/" in applied
        and not any(c.isspace() for c in applied)
    ):
        return applied
    return f"{region}-docker.pkg.dev/{project}/{REPOSITORY}"


def default_tag() -> str:
    commit = (
        run_quiet(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"]
        )
        or "dev"
    )
    dirty = run_quiet(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "status",
            "--porcelain",
            "--",
            "sandbox",
        ]
    )
    return f"{commit}-dirty" if dirty else commit


def image_uri(
    project: str,
    region: str,
    tag: str | None = None,
    repository: str | None = None,
) -> str:
    repo = repository or repository_url(project, region)
    return f"{repo}/{IMAGE_NAME}:{tag or default_tag()}"


def build_image(
    project: str,
    region: str,
    tag: str | None = None,
    repository: str | None = None,
) -> str:
    uri = image_uri(project, region, tag, repository)
    gcloud(
        "services", "enable", "artifactregistry.googleapis.com",
        "cloudbuild.googleapis.com", f"--project={project}", "--quiet",
    )  # fmt: skip
    ui.info(f"⏳ Building {uri} with Cloud Build (a few minutes)...")
    build = ["gcloud", "builds", "submit", str(_SANDBOX_DIR), f"--project={project}", f"--region={region}", f"--config={_CLOUDBUILD}", f"--substitutions=_IMAGE={uri}"]  # fmt: skip
    subprocess.run(build, check=True)  # noqa: S603 - fixed gcloud argv, no shell
    return uri


def ensure_image(
    project: str,
    region: str,
    tag: str | None = None,
    repository: str | None = None,
) -> str:
    """Return `uri`, building first unless already in Artifact Registry (`-dirty` tags always rebuild)."""
    uri = image_uri(project, region, tag, repository)
    if not uri.endswith("-dirty") and gcloud(
        "artifacts", "docker", "images", "describe", uri, f"--project={project}"
    ):  # fmt: skip
        ui.ok(f"{uri} is already in the registry")
        return uri
    return build_image(project, region, tag, repository)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="default: GOOGLE_CLOUD_PROJECT")
    parser.add_argument(
        "--region",
        default="us-central1",
        help="the deployment's region, as for make deploy (default: us-central1)",
    )
    parser.add_argument("--tag", help="default: the short git commit")
    parser.add_argument(
        "--repository",
        help="default: terraform's computer_image_repository output",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="never prompt on stdin",
    )
    args = parser.parse_args(argv)
    load_dotenv_defaults()
    if args.project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    project, _ = sandbox_project_and_location()
    uri = build_image(project, args.region, args.tag, args.repository)
    ui.ok(f"Pushed {uri}")
    print(
        "  Roll it out: make deploy COMPUTER_IMAGE="
        f"{uri}\n"
        f'  (or set computer_image = "{uri}" in terraform/terraform.tfvars).'
        "\n  Each computer is recreated from the new template on its next start;"
        " home disks are kept."
    )


if __name__ == "__main__":
    main()
