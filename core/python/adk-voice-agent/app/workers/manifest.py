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

"""Workspaces manifest (`config/workspaces.yaml` + local overrides + GitHub App repos)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def repo_slug(repo: str) -> str:
    return repo.strip().lower().replace(" ", "-")


def known_repos() -> list[dict[str, str]]:
    manifest = load_workspaces_manifest()
    repos: dict[str, str] = {}
    for entry in [
        *manifest.get("repositories", []),
        *manifest.get("seed_repositories", []),
    ]:
        if isinstance(entry, dict) and entry.get("name"):
            name = str(entry["name"])
            repos.setdefault(name, str(entry.get("description") or "").strip())
    return [{"name": n, "description": d} for n, d in repos.items()]


def repo_source(repo: str) -> tuple[str, str | None, str, dict[str, str]]:
    slug = repo_slug(repo)
    manifest = load_workspaces_manifest()
    spec = next(
        (r for r in manifest.get("repositories", []) if r.get("name") == slug),
        None,
    )
    seed = next(
        (
            r
            for r in manifest.get("seed_repositories", [])
            if r.get("name") == slug
        ),
        None,
    )
    git_url = str((spec or {}).get("git_url") or "").strip()
    upstream_url = str((spec or {}).get("upstream_url") or "").strip() or None
    branch = str((spec or seed or {}).get("branch") or "main").strip()
    raw_files = (seed or {}).get("files")
    files = raw_files if isinstance(raw_files, dict) else {}
    return (
        git_url,
        upstream_url,
        branch,
        {str(k): str(v) for k, v in files.items()},
    )


def _merge_repo_lists(
    base_list: list[dict[str, Any]], override_list: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in [*base_list, *override_list]:
        if isinstance(item, dict) and item.get("name"):
            merged.setdefault(str(item["name"]), {}).update(item)
    return list(merged.values())


def _with_installation_repos(
    repos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append the acting user's GitHub App repos; declared repos win name clashes."""
    from app.auth.github.installation_repos import installation_repos

    names = {str(r.get("name")) for r in repos}
    return [
        *repos,
        *(r for r in installation_repos() if r["name"] not in names),
    ]


def load_workspaces_manifest(
    local_override_path: Path | None = None,
) -> dict[str, Any]:
    manifest_path = _CONFIG_DIR / "workspaces.yaml"
    if not manifest_path.exists():
        return {}
    try:
        base_data: dict[str, Any] = (
            yaml.safe_load(manifest_path.read_text()) or {}
        )
    except yaml.YAMLError:
        return {}

    env_local = os.getenv("VOICE_AGENT_WORKSPACES_LOCAL_YAML")
    override_file = local_override_path or (
        Path(env_local) if env_local else _CONFIG_DIR / "workspaces.local.yaml"
    )
    local_data: dict[str, Any] = {}
    if override_file.exists():
        try:
            local_data = yaml.safe_load(override_file.read_text()) or {}
        except yaml.YAMLError:
            local_data = {}

    result = dict(base_data)

    result["repositories"] = _with_installation_repos(
        _merge_repo_lists(
            list(base_data.get("repositories", [])),
            list(local_data.get("repositories", [])),
        )
    )
    result["seed_repositories"] = _merge_repo_lists(
        list(base_data.get("seed_repositories", [])),
        list(local_data.get("seed_repositories", [])),
    )
    return result
