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

"""Repo paths and helpers shared by the provisioning modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.auth.providers import (
    LOCAL_BASE_URL,
    get_redirect_uri,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"


def redirect_uris(app: str, app_url: str | None) -> list[str]:
    return list(
        dict.fromkeys(
            get_redirect_uri(app, base_url=base)
            for base in (app_url, LOCAL_BASE_URL)
            if base
        )
    )


def run_quiet(cmd: list[str], timeout: int = 4) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 - callers pass fixed CLI argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    if proc.returncode == 0 and out and "ERROR" not in out:
        return out
    return None


def gcloud(*args: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
            ["gcloud", *args],  # noqa: S607 - gcloud resolved from PATH
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
