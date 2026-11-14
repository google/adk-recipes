#!/usr/bin/env python3
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

"""Collect segmented recipe deployment telemetry and sync it to Google Sheets.

Why this runs in GitHub Actions (and NEVER inside customer containers)
----------------------------------------------------------------------
Customer deployments run inside arbitrary third-party GCP projects and VPCs.
Having a recipe container call home to a maintainer-owned Google Sheet would
fail under customer egress controls, add latency, and create privacy risk.

Instead, live usage segmentation works through two zero-interference channels:
  1. Runtime `x-goog-api-client` / `user-agent` attribution (`gccl/` token
     carrying `<version>-<recipe>-<surface>-<channel>`) on standard Vertex AI
     and Google Cloud API calls.
  2. Artifact Registry image pull audit logs (`Docker-GetManifest`) in
     `adk-samples-repo-gcp-support`, segmented by recipe image, tag (`:latest`
     vs. pinned build tag), and caller surface (Agent Engine service agent,
     Cloud Run serverless robot, or external pull).

This script aggregates both the repository's per-recipe telemetry posture and
the segmented Artifact Registry / Cloud Logging usage metrics, writes a
Markdown summary for `$GITHUB_STEP_SUMMARY`, and syncs the table to a dedicated
worksheet tab on the target Google Sheet.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from recipe_images_matrix import REGISTRY
from recipe_manifests import REPO_ROOT, SCAN_ROOTS, SKIP_DIRS

DEFAULT_SPREADSHEET_ID = "1-Asr-QrGUaBheoTkUASroNI9UA1rCgMV6jyxWGpLa7Q"
DEFAULT_SNAPSHOT_TAB = "Recipe Telemetry"
DEFAULT_HISTORY_TAB = "Telemetry Log"
DEFAULT_GCP_PROJECT = "adk-samples-repo-gcp-support"
DEFAULT_SERVICE_ACCOUNT = (
    "wif-service-account@adk-samples-repo-gcp-support.iam.gserviceaccount.com"
)

_TELEMETRY_MARKERS = re.compile(
    r"register_adk_telemetry|build_tracking_headers|tracking_headers|"
    r"build_usage_headers|gccl/"
)
_MANIFEST_FIELD_RE = re.compile(
    r"^\s*([a-zA-Z0-9_]+)\s*:\s*([^\n#]*)", re.MULTILINE
)


@dataclass
class RecipeTelemetryRow:
    """One recipe's static telemetry posture and live segmented pull counts."""

    snapshot_utc: str
    recipe_path: str
    root: str
    category: str
    name: str
    language: str
    deployable: bool
    has_dockerfile: bool
    has_gccl_telemetry: bool
    gccl_token_pattern: str
    published_tags_count: int
    latest_build_tag: str
    pulls_window_total: int
    pulls_latest_tag: int
    pulls_pinned_tag: int
    pulls_agent_engine: int
    pulls_cloud_run: int
    pulls_external: int

    def to_sheet_row(self) -> list[Any]:
        return [
            self.snapshot_utc,
            self.recipe_path,
            self.root,
            self.category,
            self.name,
            self.language,
            "true" if self.deployable else "false",
            "true" if self.has_dockerfile else "false",
            "true" if self.has_gccl_telemetry else "false",
            self.gccl_token_pattern,
            self.published_tags_count,
            self.latest_build_tag,
            self.pulls_window_total,
            self.pulls_latest_tag,
            self.pulls_pinned_tag,
            self.pulls_agent_engine,
            self.pulls_cloud_run,
            self.pulls_external,
        ]


SHEET_HEADERS: list[str] = [
    "Snapshot (UTC)",
    "Recipe Path",
    "Root",
    "Category",
    "Recipe Name",
    "Language",
    "Deployable",
    "Has Dockerfile",
    "Has gccl/ Telemetry",
    "Expected gccl/ Token Pattern",
    "Published Image Tags",
    "Latest Build Tag",
    "Pulls (Window Total)",
    "Pulls (:latest)",
    "Pulls (Pinned Tag)",
    "Pulls (Agent Engine)",
    "Pulls (Cloud Run)",
    "Pulls (External / Direct)",
]


def _parse_manifest_simple(manifest_path: Path) -> dict[str, str]:
    """Parse top-level scalar fields from manifest.yaml without PyYAML."""
    out: dict[str, str] = {}
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return out
    for match in _MANIFEST_FIELD_RE.finditer(text):
        key = match.group(1).strip()
        val = match.group(2).strip().strip("\"'")
        if val:
            out[key] = val
    return out


def _recipe_has_gccl_telemetry(recipe_dir: Path) -> bool:
    """Return True if any source file in `recipe_dir` wires `gccl/` telemetry."""
    for path in recipe_dir.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if _TELEMETRY_MARKERS.search(
                path.read_text(encoding="utf-8", errors="ignore")
            ):
                return True
        except OSError:
            continue
    return False


def discover_recipes(repo_root: Path) -> list[dict[str, Any]]:
    """Discover all active recipes under SCAN_ROOTS."""
    recipes: list[dict[str, Any]] = []
    for root in SCAN_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for manifest in sorted(base.rglob("manifest.yaml")):
            recipe_dir = manifest.parent
            rel = recipe_dir.relative_to(repo_root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            parts = rel.parts
            category = parts[1] if len(parts) >= 3 else parts[0]
            name = parts[-1]
            meta = _parse_manifest_simple(manifest)
            language = meta.get("language", category)
            deployable = meta.get("deployable", "").lower() == "true"
            has_dockerfile = (recipe_dir / "Dockerfile").is_file()
            has_gccl = _recipe_has_gccl_telemetry(recipe_dir)
            recipes.append(
                {
                    "path": str(rel),
                    "root": parts[0],
                    "category": category,
                    "name": name,
                    "language": language,
                    "deployable": deployable,
                    "has_dockerfile": has_dockerfile,
                    "has_gccl_telemetry": has_gccl,
                }
            )
    return recipes


def _http_json(
    method: str,
    url: str,
    token: str | None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    """Execute an HTTP JSON request using stdlib urllib."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw.strip() else {"error": raw}
        except json.JSONDecodeError:
            body = {"error": raw[:500]}
        return exc.code, body
    except Exception as exc:
        return 0, {"error": str(exc)}


def fetch_registry_tags(
    category: str, name: str, token: str | None
) -> tuple[int, str]:
    """Query Artifact Registry Docker v2 API for a recipe's published tags."""
    host, _, repo_path = REGISTRY.partition("/")
    url = f"https://{host}/v2/{repo_path}/{category}/{name}/tags/list"
    status, body = _http_json("GET", url, token)
    if status != 200:
        return 0, ""
    tags = [str(t) for t in (body.get("tags") or []) if t]
    dated = sorted(t for t in tags if t != "latest")
    latest_build = (
        dated[-1] if dated else ("latest" if "latest" in tags else "")
    )
    return len(tags), latest_build


def classify_pull_entry(entry: dict[str, Any]) -> tuple[str, str, str] | None:
    """Extract (image_key, tag_bucket, caller_bucket) from a Cloud Logging entry.

    Returns:
        `(category/name, "latest" | "pinned", "agent_engine" | "cloud_run" | "external")`
        or None if the log entry does not name a recipe image.
    """
    proto = entry.get("protoPayload") or {}
    resource_name = str(proto.get("resourceName") or "")
    # Resource names look like:
    # projects/.../repositories/adk-recipes-registry/dockerImages/python%2Fdeep-search@sha256:...
    # or carry the path in request/metadata.
    match = re.search(
        r"adk-recipes-registry/(?:dockerImages/|manifests/)?([a-z0-9_-]+(?:/|%2F)[a-z0-9_-]+)(?:[:@]([^\s/\"']+))?",
        resource_name,
        re.IGNORECASE,
    )
    if not match:
        return None
    image_key = urllib.parse.unquote(match.group(1)).lower()
    ref = (match.group(2) or "latest").lower()
    tag_bucket = (
        "latest" if ref == "latest" or ref.startswith("sha256:") else "pinned"
    )

    auth = proto.get("authenticationInfo") or {}
    principal = str(auth.get("principalEmail") or "").lower()
    req_meta = proto.get("requestMetadata") or {}
    ua = str(req_meta.get("callerSuppliedUserAgent") or "").lower()

    if "gcp-sa-aiplatform" in principal or "reasoning-engine" in ua:
        caller_bucket = "agent_engine"
    elif "serverless-robot-prod" in principal or "cloud-run" in ua:
        caller_bucket = "cloud_run"
    else:
        caller_bucket = "external"

    return image_key, tag_bucket, caller_bucket


def fetch_pull_counts(
    project_id: str, lookback_days: int, token: str | None
) -> dict[str, dict[str, int]]:
    """Query Cloud Logging for Artifact Registry pull events by recipe image."""
    if not token:
        return {}
    since = (
        dt.datetime.now(dt.UTC) - dt.timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_filter = (
        'protoPayload.serviceName="artifactregistry.googleapis.com" AND '
        '(protoPayload.methodName="Docker-GetManifest" OR '
        'protoPayload.methodName="google.devtools.artifactregistry.v1.ArtifactRegistry.GetTag") AND '
        f'timestamp>="{since}"'
    )
    counts: dict[str, dict[str, int]] = {}
    page_token: str | None = None
    for _ in range(10):  # cap pagination at 10 pages (10,000 entries)
        payload: dict[str, Any] = {
            "resourceNames": [f"projects/{project_id}"],
            "filter": log_filter,
            "pageSize": 1000,
        }
        if page_token:
            payload["pageToken"] = page_token
        status, body = _http_json(
            "POST",
            "https://logging.googleapis.com/v2/entries:list",
            token,
            payload,
        )
        if status != 200:
            print(
                f"::warning::Cloud Logging query returned HTTP {status}; "
                "proceeding with 0 pull counts. Ensure logging.googleapis.com "
                "is enabled and wif-service-account has roles/logging.viewer.",
                file=sys.stderr,
            )
            break
        for entry in body.get("entries") or []:
            parsed = classify_pull_entry(entry)
            if not parsed:
                continue
            image_key, tag_bucket, caller_bucket = parsed
            bucket = counts.setdefault(
                image_key,
                {
                    "total": 0,
                    "latest": 0,
                    "pinned": 0,
                    "agent_engine": 0,
                    "cloud_run": 0,
                    "external": 0,
                },
            )
            bucket["total"] += 1
            bucket[tag_bucket] += 1
            bucket[caller_bucket] += 1
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return counts


def collect_telemetry_rows(
    repo_root: Path,
    *,
    project_id: str = DEFAULT_GCP_PROJECT,
    lookback_days: int = 30,
    token: str | None = None,
    now_utc: str | None = None,
) -> list[RecipeTelemetryRow]:
    """Build the complete segmented telemetry table across all active recipes."""
    timestamp = now_utc or dt.datetime.now(dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    recipes = discover_recipes(repo_root)
    pull_map = fetch_pull_counts(project_id, lookback_days, token)
    rows: list[RecipeTelemetryRow] = []

    for r in recipes:
        image_key = f"{r['category']}/{r['name']}".lower()
        tags_count, latest_tag = (0, "")
        if r["has_dockerfile"] and token:
            tags_count, latest_tag = fetch_registry_tags(
                r["category"], r["name"], token
            )
        pulls = pull_map.get(image_key, {})
        pattern = f"gccl/<ver>-{r['name']}-<surface>-<channel>"
        rows.append(
            RecipeTelemetryRow(
                snapshot_utc=timestamp,
                recipe_path=r["path"],
                root=r["root"],
                category=r["category"],
                name=r["name"],
                language=r["language"],
                deployable=r["deployable"],
                has_dockerfile=r["has_dockerfile"],
                has_gccl_telemetry=r["has_gccl_telemetry"],
                gccl_token_pattern=pattern,
                published_tags_count=tags_count,
                latest_build_tag=latest_tag,
                pulls_window_total=pulls.get("total", 0),
                pulls_latest_tag=pulls.get("latest", 0),
                pulls_pinned_tag=pulls.get("pinned", 0),
                pulls_agent_engine=pulls.get("agent_engine", 0),
                pulls_cloud_run=pulls.get("cloud_run", 0),
                pulls_external=pulls.get("external", 0),
            )
        )
    return rows


def sync_to_google_sheet(
    spreadsheet_id: str,
    rows: list[RecipeTelemetryRow],
    token: str,
    *,
    snapshot_tab: str = DEFAULT_SNAPSHOT_TAB,
    history_tab: str = DEFAULT_HISTORY_TAB,
) -> bool:
    """Create missing tabs and sync the snapshot + history rows to Google Sheets."""
    meta_url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        "?fields=sheets.properties.title"
    )
    status, meta = _http_json("GET", meta_url, token)
    if status != 200:
        print(
            f"::warning::Could not access Google Sheet {spreadsheet_id} "
            f"(HTTP {status}). Share the spreadsheet with "
            f"{DEFAULT_SERVICE_ACCOUNT} as Editor and ensure "
            "sheets.googleapis.com is enabled.",
            file=sys.stderr,
        )
        return False

    existing_tabs = {
        s.get("properties", {}).get("title") for s in (meta.get("sheets") or [])
    }
    requests: list[dict[str, Any]] = []
    for tab in (snapshot_tab, history_tab):
        if tab not in existing_tabs:
            requests.append({"addSheet": {"properties": {"title": tab}}})

    if requests:
        batch_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
            ":batchUpdate"
        )
        b_status, b_body = _http_json(
            "POST", batch_url, token, {"requests": requests}
        )
        if b_status != 200:
            print(
                f"::warning::Failed to create worksheet tab(s) (HTTP {b_status}): "
                f"{b_body}",
                file=sys.stderr,
            )
            return False

    values = [SHEET_HEADERS] + [r.to_sheet_row() for r in rows]

    # 1. Clear and write the live snapshot tab
    snap_range = urllib.parse.quote(f"'{snapshot_tab}'!A1")
    clear_range = urllib.parse.quote(f"'{snapshot_tab}'!A:Z")
    _http_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{clear_range}:clear",
        token,
        {},
    )
    u_status, u_body = _http_json(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{snap_range}?valueInputOption=USER_ENTERED",
        token,
        {"values": values},
    )
    if u_status != 200:
        print(
            f"::warning::Failed to update '{snapshot_tab}' tab (HTTP {u_status}): "
            f"{u_body}",
            file=sys.stderr,
        )
        return False

    # 2. Append to the history tab (write headers first if newly created)
    hist_range = urllib.parse.quote(f"'{history_tab}'!A1")
    hist_rows = (
        values
        if history_tab not in existing_tabs
        else [r.to_sheet_row() for r in rows]
    )
    if hist_rows:
        _http_json(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{hist_range}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS",
            token,
            {"values": hist_rows},
        )

    return True


def render_markdown_summary(
    rows: list[RecipeTelemetryRow],
    *,
    lookback_days: int,
    sheet_synced: bool,
    spreadsheet_id: str,
) -> str:
    """Render a concise GitHub Step Summary markdown report."""
    n_total = len(rows)
    n_deployable = sum(1 for r in rows if r.deployable)
    n_docker = sum(1 for r in rows if r.has_dockerfile)
    n_gccl = sum(1 for r in rows if r.has_gccl_telemetry)
    total_pulls = sum(r.pulls_window_total for r in rows)

    lines = [
        "## 📊 Recipe Deployment & Usage Telemetry",
        "",
        f"- **Active recipes scanned:** {n_total}",
        f"- **Containerized (`Dockerfile`):** {n_docker} ({n_deployable} marked `deployable: true`)",
        f"- **Instrumented with `gccl/` runtime header:** {n_gccl}",
        f"- **Artifact Registry pulls (last {lookback_days}d):** {total_pulls}",
        f"- **Google Sheet sync (`{spreadsheet_id}`):** "
        + (
            "Synced" if sheet_synced else "Skipped / pending Sheet Editor grant"
        ),
        "",
        "| Recipe | Lang | Deployable | Dockerfile | `gccl/` Header | Latest Tag | Pulls (Total / AE / CR / Ext) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        pulls_str = (
            f"{r.pulls_window_total} "
            f"({r.pulls_agent_engine} / {r.pulls_cloud_run} / {r.pulls_external})"
        )
        lines.append(
            f"| `{r.recipe_path}` | {r.language} | "
            f"{'yes' if r.deployable else 'no'} | "
            f"{'yes' if r.has_dockerfile else 'no'} | "
            f"{'yes' if r.has_gccl_telemetry else 'no'} | "
            f"`{r.latest_build_tag or '-'}` | {pulls_str} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spreadsheet-id",
        default=DEFAULT_SPREADSHEET_ID,
        help="Target Google Sheet ID.",
    )
    parser.add_argument(
        "--snapshot-tab",
        default=DEFAULT_SNAPSHOT_TAB,
        help="Worksheet tab name for the latest snapshot.",
    )
    parser.add_argument(
        "--history-tab",
        default=DEFAULT_HISTORY_TAB,
        help="Worksheet tab name for the append-only history log.",
    )
    parser.add_argument(
        "--project-id",
        default=DEFAULT_GCP_PROJECT,
        help="GCP project hosting the Artifact Registry and Cloud Logging.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Days of Artifact Registry pull logs to aggregate.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("GCP_ACCESS_TOKEN", ""),
        help="OAuth2 access token with cloud-platform and spreadsheets scopes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and print telemetry without writing to Google Sheets.",
    )
    parser.add_argument(
        "--out-json",
        help="Optional path to write the collected rows as JSON.",
    )
    parser.add_argument(
        "--out-md",
        help="Optional path to write the Markdown summary report.",
    )
    args = parser.parse_args()

    token = args.access_token.strip() or None
    rows = collect_telemetry_rows(
        REPO_ROOT,
        project_id=args.project_id,
        lookback_days=args.lookback_days,
        token=token,
    )

    sheet_synced = False
    if not args.dry_run and token and args.spreadsheet_id:
        sheet_synced = sync_to_google_sheet(
            args.spreadsheet_id,
            rows,
            token,
            snapshot_tab=args.snapshot_tab,
            history_tab=args.history_tab,
        )

    md = render_markdown_summary(
        rows,
        lookback_days=args.lookback_days,
        sheet_synced=sheet_synced,
        spreadsheet_id=args.spreadsheet_id,
    )

    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps([asdict(r) for r in rows], indent=2) + "\n",
            encoding="utf-8",
        )
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")

    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
