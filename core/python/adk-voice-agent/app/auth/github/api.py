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

"""GitHub REST API client, headers, and remote URL parsing."""

from __future__ import annotations

import re

import httpx

from app.app_utils.http_client import get_http_client

GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_PROVIDER = "github"

_GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_client(
    token: str | None = None, *, timeout: float = 15.0
) -> httpx.AsyncClient:
    return get_http_client(timeout=timeout, headers=github_headers(token))


def parse_github_remote(git_url: str | None) -> tuple[str, str] | None:
    match = _GITHUB_REMOTE.match((git_url or "").strip())
    return (match.group(1), match.group(2)) if match else None
