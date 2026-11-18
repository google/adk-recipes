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

"""Cached GitHub App installation repositories for the acting user."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.auth.credential_store import credential_owner_or_none
from app.auth.github.api import GITHUB_API, GITHUB_PROVIDER, github_client

logger = logging.getLogger(__name__)

INSTALLATION_REPOS_TTL_S = 300.0
_user_repos: dict[str, tuple[float, list[dict[str, str]]]] = {}
_background: set[asyncio.Task[Any]] = set()


class GitHubAppError(RuntimeError):
    pass


def has_app_connection() -> bool:
    from app.auth.providers import has_provider_credentials

    if credential_owner_or_none() is None:
        return False
    return has_provider_credentials(GITHUB_PROVIDER)


def _repo_entries(repos: list[dict[str, Any]]) -> list[dict[str, str]]:
    from app.workers.manifest import repo_slug

    names = [repo_slug(str(r.get("name") or "")) for r in repos]
    entries: dict[str, dict[str, str]] = {}
    for repo, repo_name in zip(repos, names, strict=True):
        full_name = str(repo.get("full_name") or "")
        if not repo_name or "/" not in full_name:
            continue
        # Disambiguate colliding repo names across owners with the owner prefix.
        name = (
            repo_slug(full_name.replace("/", "-"))
            if names.count(repo_name) > 1
            else repo_name
        )
        entries.setdefault(
            name,
            {
                "name": name,
                "git_url": str(
                    repo.get("clone_url")
                    or f"https://github.com/{full_name}.git"
                ),
                "branch": str(repo.get("default_branch") or "main"),
                "description": str(repo.get("description") or "").strip(),
                "source": "github_app",
            },
        )
    return list(entries.values())


async def _fetch_installation_repos(token: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    async with github_client(token) as client:
        resp = await client.get(
            f"{GITHUB_API}/user/installations", params={"per_page": 100}
        )
        if resp.status_code != 200:
            raise GitHubAppError(
                f"GitHub returned {resp.status_code} listing your App installations."
            )
        for installation in resp.json().get("installations") or []:
            listing = await client.get(
                f"{GITHUB_API}/user/installations/{int(installation['id'])}/repositories",
                params={"per_page": 100},
            )
            if listing.status_code != 200:
                raise GitHubAppError(
                    f"GitHub returned {listing.status_code} listing installation repos."
                )
            repos.extend(
                r
                for r in listing.json().get("repositories") or []
                if isinstance(r, dict)
            )
    return repos


async def refresh_installation_repos(
    *, force: bool = False
) -> list[dict[str, str]]:
    from app.auth.credential_store import prefetch_user_credentials
    from app.auth.oauth import ensure_fresh_access_token

    user = credential_owner_or_none()
    if user is None:
        return []
    await prefetch_user_credentials()
    if not has_app_connection():
        _user_repos.pop(user, None)
        return []
    hit = _user_repos.get(user)
    if (
        hit is not None
        and not force
        and time.monotonic() - hit[0] < INSTALLATION_REPOS_TTL_S
    ):
        return hit[1]
    token = await ensure_fresh_access_token(GITHUB_PROVIDER)
    if not token:
        return []
    try:
        entries = _repo_entries(await _fetch_installation_repos(token))
    except httpx.HTTPError as exc:
        raise GitHubAppError(f"Could not reach GitHub: {exc}") from exc
    _user_repos[user] = (time.monotonic(), entries)
    return entries


async def warm_installation_repos() -> None:
    user = credential_owner_or_none()
    if user is None or not has_app_connection():
        return
    hit = _user_repos.get(user)
    if hit is None:
        await refresh_installation_repos()
    elif time.monotonic() - hit[0] >= INSTALLATION_REPOS_TTL_S:
        task = asyncio.create_task(_refresh_quietly())
        _background.add(task)
        task.add_done_callback(_background.discard)


async def _refresh_quietly() -> None:
    try:
        await refresh_installation_repos()
    except Exception as exc:
        logger.warning("Could not refresh GitHub installation repos: %s", exc)


def installation_repos(user_id: str | None = None) -> list[dict[str, str]]:
    if not _user_repos:
        return []
    user = user_id or credential_owner_or_none()
    if user is None:
        return []
    hit = _user_repos.get(user)
    return [dict(r) for r in hit[1]] if hit else []


def reset_installation_repos() -> None:
    _user_repos.clear()
