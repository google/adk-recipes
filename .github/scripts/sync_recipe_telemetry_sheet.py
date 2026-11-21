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
  2. Artifact Registry image pull audit logs (`Docker-GetManifest` / `GetTag`)
     in `adk-samples-repo-gcp-support`, segmented by recipe image, tag (`:latest`
     vs. pinned build tag / digest), and caller surface (Agent Engine service
     agent, Cloud Run serverless robot, or external pull).

This script aggregates both the repository's per-recipe telemetry posture and
the segmented Artifact Registry / Cloud Logging usage metrics, writes a
Markdown summary for `$GITHUB_STEP_SUMMARY`, and syncs the table to the target
Google Sheet.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from recipe_images_matrix import REGISTRY
from recipe_manifests import REPO_ROOT, SCAN_ROOTS, SKIP_DIRS

DEFAULT_SPREADSHEET_ID = "1-Asr-QrGUaBheoTkUASroNI9UA1rCgMV6jyxWGpLa7Q"
DEFAULT_SHEET_GID = "567149855"
DEFAULT_SNAPSHOT_TAB = "Recipe Telemetry"
DEFAULT_HISTORY_TAB = "Telemetry Log"
DEFAULT_GCP_PROJECT = "adk-samples-repo-gcp-support"
DEFAULT_SERVICE_ACCOUNT = (
    "wif-service-account@adk-samples-repo-gcp-support.iam.gserviceaccount.com"
)

_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

_TELEMETRY_MARKERS = re.compile(
    r"\b(?:register_adk_telemetry|build_tracking_headers|build_usage_headers)\b|"
    r"def\s+tracking_headers\b|gccl/"
)
_MANIFEST_FIELD_RE = re.compile(
    r"^([a-zA-Z0-9_]+)[ \t]*:[ \t]*([^\r\n#]*)", re.MULTILINE
)
_DATED_BUILD_TAG_RE = re.compile(r"^\d{8}-[0-9a-f]+$", re.IGNORECASE)
_PRUNE_DIRS = SKIP_DIRS | {
    "venv",
    "tests",
    "test",
    ".pytest_cache",
    ".ruff_cache",
}
_SOURCE_EXTS = {".py", ".go", ".java", ".kt", ".ts"}


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
            self.deployable,
            self.has_dockerfile,
            self.has_gccl_telemetry,
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
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for match in _MANIFEST_FIELD_RE.finditer(text):
        key = match.group(1).strip()
        val = match.group(2).strip().strip("\"'")
        if val:
            out[key] = val
    return out


def _recipe_has_gccl_telemetry(recipe_dir: Path) -> bool:
    """Return True if any non-test source file in `recipe_dir` wires `gccl/` telemetry."""
    for dirpath, dirnames, filenames in os.walk(recipe_dir):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fname in filenames:
            path = Path(dirpath) / fname
            if path.suffix not in _SOURCE_EXTS:
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
            meta = _parse_manifest_simple(manifest)
            if meta.get("status", "active").lower() == "inactive":
                continue
            parts = rel.parts
            category = parts[1] if len(parts) >= 3 else parts[0]
            name = parts[-1]
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


def _validate_spreadsheet_id(spreadsheet_id: str) -> str:
    """Validate Google Sheet ID format before interpolating into API URLs."""
    cleaned = spreadsheet_id.strip()
    if not _SPREADSHEET_ID_RE.match(cleaned):
        raise ValueError(f"Invalid Google Sheet ID: {spreadsheet_id!r}")
    return cleaned


def _validate_project_id(project_id: str) -> str:
    """Validate GCP project ID format before interpolating into URLs/filters."""
    cleaned = project_id.strip()
    if not _PROJECT_ID_RE.match(cleaned):
        raise ValueError(f"Invalid GCP project ID: {project_id!r}")
    return cleaned


def _col_letter(n: int) -> str:
    """Convert a 1-based column index (e.g. 18 -> 'R') to A1 column letters."""
    if n < 1:
        raise ValueError(f"Column index must be >= 1, got {n}")
    chars: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _md_cell(value: str) -> str:
    """Escape pipe and backtick characters for a Markdown table cell."""
    return value.replace("|", "\\|").replace("`", "'")


def _http_json(
    method: str,
    url: str,
    token: str | None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    *,
    idempotent: bool | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute an HTTP JSON request using stdlib urllib with 429/5xx backoff.

    Only retries idempotent requests (GET/PUT/HEAD/OPTIONS/DELETE by default,
    or POST calls explicitly marked ``idempotent=True``) so non-idempotent
    writes like ``:append`` or ``:batchUpdate`` are never duplicated on timeout.
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    can_retry = (
        method.upper() in ("GET", "PUT", "HEAD", "OPTIONS", "DELETE")
        if idempotent is None
        else idempotent
    )
    max_attempts = 4 if can_retry else 1

    for attempt in range(max_attempts):
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return resp.status, {}
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, {"error": f"invalid json: {raw[:200]}"}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if (
                exc.code in (429, 500, 502, 503, 504)
                and attempt < max_attempts - 1
            ):
                time.sleep(2**attempt)
                continue
            try:
                body = json.loads(raw) if raw.strip() else {"error": raw}
            except json.JSONDecodeError:
                body = {"error": raw[:500]}
            return exc.code, body
        except Exception as exc:
            if attempt < max_attempts - 1:
                time.sleep(2**attempt)
                continue
            return 0, {"error": str(exc)}
    return 0, {"error": "retries exhausted"}


def _registry_coords() -> tuple[str, str, str]:
    """Parse (location, project_id, repo_name) from REGISTRY."""
    host, _, repo_path = REGISTRY.partition("/")
    location = host.removesuffix("-docker.pkg.dev")
    project_id, _, repo_name = repo_path.partition("/")
    return location, project_id, repo_name


def fetch_registry_tags(
    category: str,
    name: str,
    token: str | None,
    *,
    project_id: str | None = None,
) -> tuple[int, str]:
    """Query Artifact Registry REST API v1 for a recipe's published tags."""
    location, default_project_id, repo_name = _registry_coords()
    target_project = _validate_project_id(project_id or default_project_id)
    q_project = urllib.parse.quote(target_project, safe="")
    q_location = urllib.parse.quote(location, safe="")
    q_repo = urllib.parse.quote(repo_name, safe="")
    pkg = urllib.parse.quote(f"{category}/{name}", safe="")
    base_url = (
        f"https://artifactregistry.googleapis.com/v1/projects/{q_project}"
        f"/locations/{q_location}/repositories/{q_repo}/packages/{pkg}/tags"
    )
    tags: list[str] = []
    page_token = ""
    for _ in range(10):
        url = f"{base_url}?pageSize=100"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
        status, body = _http_json("GET", url, token)
        if status != 200:
            if status != 404:
                print(
                    f"warning: Artifact Registry tag lookup for {category}/{name} "
                    f"returned HTTP {status}: {body}",
                    file=sys.stderr,
                )
            if not tags:
                return 0, ""
            break
        for item in body.get("tags") or []:
            tag_res = (
                str(item.get("name") or "")
                if isinstance(item, dict)
                else str(item)
            )
            leaf = tag_res.rsplit("/tags/", 1)[-1].strip()
            if leaf:
                tags.append(leaf)
        page_token = str(body.get("nextPageToken") or "")
        if not page_token:
            break
    else:
        if page_token:
            print(
                f"warning: Artifact Registry tags for {category}/{name} "
                "exceeded 10 pages (1,000 tags); count may be truncated.",
                file=sys.stderr,
            )

    if not tags:
        return 0, ""
    dated = sorted(t for t in tags if _DATED_BUILD_TAG_RE.match(t))
    other = sorted(
        t for t in tags if t != "latest" and not _DATED_BUILD_TAG_RE.match(t)
    )
    latest_build = (
        dated[-1]
        if dated
        else (other[-1] if other else ("latest" if "latest" in tags else ""))
    )
    return len(tags), latest_build


@functools.lru_cache(maxsize=8)
def _pull_resource_patterns(
    repo_name: str,
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Build pull resource regexes from the Artifact Registry repository name."""
    escaped_repo = re.escape(repo_name)
    return (
        # GetTag / GetVersion: .../repositories/<repo>/packages/python%2Fdeep-search/tags/20261114-...
        re.compile(
            rf"{escaped_repo}/packages/([a-z0-9_-]+(?:/|%2F)[a-z0-9_-]+)/(?:tags|versions)/([^\s/\"']+)",
            re.IGNORECASE,
        ),
        # Docker-GetManifest: .../repositories/<repo>/dockerImages/python%2Fdeep-search:tag or @sha256:...
        re.compile(
            rf"{escaped_repo}/(?:dockerImages|manifests)/([a-z0-9_-]+(?:/|%2F)[a-z0-9_-]+)(?:[:@]([^\s/\"']+))?",
            re.IGNORECASE,
        ),
    )


def classify_pull_entry(
    entry: dict[str, Any],
    *,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
) -> tuple[str, str, str] | None:
    """Extract (image_key, tag_bucket, caller_bucket) from a Cloud Logging entry.

    Returns:
        `(category/name, "latest" | "pinned", "agent_engine" | "cloud_run" | "external")`
        or None if the log entry does not name a recipe image or is a CI self-pull.
    """
    proto = entry.get("protoPayload") or {}
    auth = proto.get("authenticationInfo") or {}
    principal = str(auth.get("principalEmail") or "").lower()
    req_meta = proto.get("requestMetadata") or {}
    ua = str(req_meta.get("callerSuppliedUserAgent") or "").lower()

    # Ignore CI self-pulls (e.g. Trivy vulnerability scans in recipe-images.yml)
    if (
        service_account and principal == service_account.lower()
    ) or "trivy" in ua:
        return None

    candidates = [
        str(proto.get("resourceName") or ""),
        json.dumps(proto.get("request") or {}),
        json.dumps(proto.get("metadata") or {}),
    ]
    image_key = ""
    ref = ""
    fallback_key = ""
    _, _, repo_name = _registry_coords()
    patterns = _pull_resource_patterns(repo_name)
    for text in candidates:
        for pat in patterns:
            match = pat.search(text)
            if not match:
                continue
            raw_key = urllib.parse.unquote(match.group(1)).lower()
            raw_ref = (match.group(2) or "").lower()
            if raw_ref:
                image_key = raw_key
                ref = raw_ref
                break
            if not fallback_key:
                fallback_key = raw_key
        if image_key:
            break

    if not image_key:
        if not fallback_key:
            return None
        image_key = fallback_key
        ref = "latest"

    tag_bucket = "latest" if ref == "latest" else "pinned"

    if "gcp-sa-aiplatform" in principal or "reasoning-engine" in ua:
        caller_bucket = "agent_engine"
    elif "serverless-robot-prod" in principal or "cloud-run" in ua:
        caller_bucket = "cloud_run"
    else:
        caller_bucket = "external"

    return image_key, tag_bucket, caller_bucket


def fetch_pull_counts(
    project_id: str,
    lookback_days: int,
    token: str | None,
    *,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
) -> dict[str, dict[str, int]]:
    """Query Cloud Logging for Artifact Registry pull events by recipe image."""
    if not token:
        return {}
    valid_project = _validate_project_id(project_id)
    since = (
        dt.datetime.now(dt.UTC) - dt.timedelta(days=lookback_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_filter = (
        f'logName="projects/{valid_project}/logs/cloudaudit.googleapis.com%2Fdata_access" AND '
        'protoPayload.serviceName="artifactregistry.googleapis.com" AND '
        '(protoPayload.methodName="Docker-GetManifest" OR '
        'protoPayload.methodName="google.devtools.artifactregistry.v1.ArtifactRegistry.GetTag") AND '
        f'timestamp>="{since}"'
    )
    counts: dict[str, dict[str, int]] = {}
    page_token: str | None = None
    for _ in range(10):  # cap pagination at 10 pages (10,000 newest entries)
        payload: dict[str, Any] = {
            "resourceNames": [f"projects/{valid_project}"],
            "filter": log_filter,
            "orderBy": "timestamp desc",
            "pageSize": 1000,
        }
        if page_token:
            payload["pageToken"] = page_token
        status, body = _http_json(
            "POST",
            "https://logging.googleapis.com/v2/entries:list",
            token,
            payload,
            idempotent=True,
        )
        if status != 200:
            print(
                f"warning: Cloud Logging query returned HTTP {status}; "
                "proceeding with collected pull counts. Ensure logging.googleapis.com "
                "is enabled and wif-service-account has roles/logging.viewer.",
                file=sys.stderr,
            )
            break
        for entry in body.get("entries") or []:
            parsed = classify_pull_entry(entry, service_account=service_account)
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
    else:
        if page_token:
            print(
                "warning: Cloud Logging query exceeded 10 pages (10,000 "
                "entries); pull counts may be truncated.",
                file=sys.stderr,
            )
    return counts


def collect_telemetry_rows(
    repo_root: Path,
    *,
    project_id: str = DEFAULT_GCP_PROJECT,
    lookback_days: int = 30,
    token: str | None = None,
    now_utc: str | None = None,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
) -> list[RecipeTelemetryRow]:
    """Build the complete segmented telemetry table across all active recipes."""
    timestamp = now_utc or dt.datetime.now(dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    recipes = discover_recipes(repo_root)
    pull_map = fetch_pull_counts(
        project_id, lookback_days, token, service_account=service_account
    )
    rows: list[RecipeTelemetryRow] = []

    for r in recipes:
        image_key = f"{r['category']}/{r['name']}".lower()
        tags_count, latest_tag = (0, "")
        if r["has_dockerfile"] and token:
            tags_count, latest_tag = fetch_registry_tags(
                r["category"], r["name"], token, project_id=project_id
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


def _a1_tab(tab_title: str) -> str:
    """Format a worksheet title for A1 notation, escaping single quotes."""
    escaped = tab_title.replace("'", "''")
    return f"'{escaped}'"


def sync_to_google_sheet_with_status(
    spreadsheet_id: str,
    rows: list[RecipeTelemetryRow],
    token: str,
    *,
    snapshot_tab: str = DEFAULT_SNAPSHOT_TAB,
    history_tab: str = DEFAULT_HISTORY_TAB,
    sheet_gid: str = "",
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
) -> str:
    """Create missing tabs and sync snapshot + history rows to Google Sheets.

    Returns one of ``"synced"``, ``"permission_denied"``, or ``"error"``.
    """
    try:
        valid_sheet_id = _validate_spreadsheet_id(spreadsheet_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return "error"

    q_sheet = urllib.parse.quote(valid_sheet_id, safe="")
    meta_url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{q_sheet}"
        "?fields=sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"
    )
    status, meta = _http_json("GET", meta_url, token)
    if status != 200:
        print(
            f"warning: Could not access Google Sheet {valid_sheet_id} "
            f"(HTTP {status}). Share the spreadsheet with "
            f"{service_account} as Editor and ensure "
            "sheets.googleapis.com is enabled.",
            file=sys.stderr,
        )
        return "permission_denied" if status in (401, 403) else "error"

    sheets_list = meta.get("sheets") or []
    existing_tabs: set[str] = set()
    tab_row_counts: dict[str, int | None] = {}
    for s in sheets_list:
        props = s.get("properties") or {}
        title = props.get("title")
        if title:
            title_str = str(title)
            existing_tabs.add(title_str)
            grid = props.get("gridProperties") or {}
            rc = grid.get("rowCount")
            tab_row_counts[title_str] = int(rc) if rc is not None else None

    # If a target GID (e.g. 567149855) is provided and exists in the workbook,
    # resolve its current title dynamically.
    resolved_snapshot_tab = snapshot_tab
    if sheet_gid and sheet_gid.strip():
        gid_str = sheet_gid.strip()
        if not gid_str.isdigit():
            print(
                f"warning: Ignoring non-numeric sheet_gid {sheet_gid!r}; "
                f"falling back to snapshot tab '{snapshot_tab}'.",
                file=sys.stderr,
            )
        else:
            wanted_gid = int(gid_str)
            matched = False
            for s in sheets_list:
                props = s.get("properties") or {}
                if (
                    props.get("sheetId") is not None
                    and int(props["sheetId"]) == wanted_gid
                ):
                    resolved_snapshot_tab = str(props["title"])
                    matched = True
                    break
            if not matched:
                print(
                    f"warning: Worksheet GID {wanted_gid} not found in "
                    f"spreadsheet {valid_sheet_id}; falling back to snapshot "
                    f"tab '{snapshot_tab}'.",
                    file=sys.stderr,
                )

    if resolved_snapshot_tab == history_tab:
        print(
            f"error: Snapshot tab and history tab both resolve to "
            f"'{resolved_snapshot_tab}'; refusing to overwrite history.",
            file=sys.stderr,
        )
        return "error"

    requests: list[dict[str, Any]] = []
    for tab in (resolved_snapshot_tab, history_tab):
        if tab not in existing_tabs:
            requests.append({"addSheet": {"properties": {"title": tab}}})

    if requests:
        batch_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{q_sheet}"
            ":batchUpdate"
        )
        b_status, b_body = _http_json(
            "POST", batch_url, token, {"requests": requests}
        )
        if b_status != 200:
            print(
                f"warning: Failed to create worksheet tab(s) (HTTP {b_status}): "
                f"{b_body}",
                file=sys.stderr,
            )
            return "permission_denied" if b_status in (401, 403) else "error"

    values = [SHEET_HEADERS] + [r.to_sheet_row() for r in rows]
    snap_a1 = _a1_tab(resolved_snapshot_tab)
    end_col = _col_letter(len(SHEET_HEADERS))

    # 1. Write the live snapshot tab FIRST, then clear only trailing rows in
    # A:<end_col> so a transient failure never leaves the sheet blank and
    # user-owned columns beyond <end_col> stay intact.
    snap_range = urllib.parse.quote(f"{snap_a1}!A1", safe="")
    u_status, u_body = _http_json(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{q_sheet}/values/{snap_range}?valueInputOption=RAW",
        token,
        {"values": values},
    )
    if u_status != 200:
        print(
            f"warning: Failed to update '{resolved_snapshot_tab}' tab (HTTP {u_status}): "
            f"{u_body}",
            file=sys.stderr,
        )
        return "permission_denied" if u_status in (401, 403) else "error"

    clear_start_row = len(values) + 1
    existing_row_count = tab_row_counts.get(resolved_snapshot_tab)
    if existing_row_count is None or clear_start_row <= existing_row_count:
        clear_range = urllib.parse.quote(
            f"{snap_a1}!A{clear_start_row}:{end_col}", safe=""
        )
        c_status, c_body = _http_json(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{q_sheet}/values/{clear_range}:clear",
            token,
            {},
            idempotent=True,
        )
        if c_status != 200:
            print(
                f"warning: Failed to clear trailing rows on '{resolved_snapshot_tab}' "
                f"tab (HTTP {c_status}): {c_body}",
                file=sys.stderr,
            )
            return "permission_denied" if c_status in (401, 403) else "error"

    # 2. Append to the history tab (write headers first if newly created or empty)
    hist_a1 = _a1_tab(history_tab)
    need_headers = history_tab not in existing_tabs
    if not need_headers:
        probe_range = urllib.parse.quote(f"{hist_a1}!A1:A1", safe="")
        p_status, p_body = _http_json(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{q_sheet}/values/{probe_range}",
            token,
        )
        if p_status == 200 and not p_body.get("values"):
            need_headers = True

    hist_range = urllib.parse.quote(f"{hist_a1}!A1", safe="")
    hist_rows = values if need_headers else [r.to_sheet_row() for r in rows]
    if hist_rows:
        a_status, a_body = _http_json(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{q_sheet}/values/{hist_range}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
            token,
            {"values": hist_rows},
        )
        if a_status != 200:
            print(
                f"warning: Failed to append to '{history_tab}' tab (HTTP {a_status}): "
                f"{a_body}",
                file=sys.stderr,
            )
            return "permission_denied" if a_status in (401, 403) else "error"

    return "synced"


def sync_to_google_sheet(
    spreadsheet_id: str,
    rows: list[RecipeTelemetryRow],
    token: str,
    *,
    snapshot_tab: str = DEFAULT_SNAPSHOT_TAB,
    history_tab: str = DEFAULT_HISTORY_TAB,
    sheet_gid: str = "",
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
) -> bool:
    """Create missing tabs and sync the snapshot + history rows to Google Sheets."""
    return (
        sync_to_google_sheet_with_status(
            spreadsheet_id,
            rows,
            token,
            snapshot_tab=snapshot_tab,
            history_tab=history_tab,
            sheet_gid=sheet_gid,
            service_account=service_account,
        )
        == "synced"
    )


def render_markdown_summary(
    rows: list[RecipeTelemetryRow],
    *,
    lookback_days: int,
    sheet_synced: bool,
    spreadsheet_id: str,
    dry_run: bool = False,
    sync_status_text: str | None = None,
) -> str:
    """Render a concise GitHub Step Summary markdown report."""
    n_total = len(rows)
    n_deployable = sum(1 for r in rows if r.deployable)
    n_docker = sum(1 for r in rows if r.has_dockerfile)
    n_gccl = sum(1 for r in rows if r.has_gccl_telemetry)
    total_pulls = sum(r.pulls_window_total for r in rows)

    if sync_status_text is not None:
        sync_status = sync_status_text
    elif sheet_synced:
        sync_status = "Synced"
    elif dry_run:
        sync_status = "Skipped (dry run)"
    else:
        sync_status = "Failed (see workflow logs)"

    lines = [
        "## 📊 Recipe Deployment & Usage Telemetry",
        "",
        f"- **Active recipes scanned:** {n_total}",
        f"- **Containerized (`Dockerfile`):** {n_docker} ({n_deployable} marked `deployable: true`)",
        f"- **Instrumented with `gccl/` runtime header:** {n_gccl}",
        f"- **Artifact Registry pulls (last {lookback_days}d):** {total_pulls}",
        f"- **Google Sheet sync (`{_md_cell(spreadsheet_id)}`):** {sync_status}",
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
            f"| `{_md_cell(r.recipe_path)}` | {_md_cell(r.language)} | "
            f"{'yes' if r.deployable else 'no'} | "
            f"{'yes' if r.has_dockerfile else 'no'} | "
            f"{'yes' if r.has_gccl_telemetry else 'no'} | "
            f"`{_md_cell(r.latest_build_tag or '-')}` | {pulls_str} |"
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
        "--sheet-gid",
        default=DEFAULT_SHEET_GID,
        help="Optional worksheet GID for the snapshot tab (e.g. 567149855).",
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
        "--service-account",
        default=DEFAULT_SERVICE_ACCOUNT,
        help=(
            "CI service account email excluded from pull counts and shown in "
            "Sheet permission hints."
        ),
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

    if args.lookback_days < 1:
        print("error: --lookback-days must be >= 1", file=sys.stderr)
        return 2

    try:
        project_id = _validate_project_id(args.project_id)
        spreadsheet_id = (
            _validate_spreadsheet_id(args.spreadsheet_id)
            if args.spreadsheet_id.strip()
            else ""
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    token = args.access_token.strip() or None
    rows = collect_telemetry_rows(
        REPO_ROOT,
        project_id=project_id,
        lookback_days=args.lookback_days,
        token=token,
        service_account=args.service_account,
    )

    if args.dry_run:
        sync_outcome = "dry_run"
        sync_status_text = "Skipped (dry run)"
    elif not token:
        sync_outcome = "no_token"
        sync_status_text = "Skipped (no access token)"
    elif not spreadsheet_id:
        sync_outcome = "no_sheet"
        sync_status_text = "Skipped (no spreadsheet ID)"
    else:
        sync_outcome = sync_to_google_sheet_with_status(
            spreadsheet_id,
            rows,
            token,
            snapshot_tab=args.snapshot_tab,
            history_tab=args.history_tab,
            sheet_gid=args.sheet_gid,
            service_account=args.service_account,
        )
        sync_status_text = {
            "synced": "Synced",
            "permission_denied": "Skipped (permission denied — grant Sheet Editor access)",
            "error": "Failed (see workflow logs)",
        }[sync_outcome]

    md = render_markdown_summary(
        rows,
        lookback_days=args.lookback_days,
        sheet_synced=(sync_outcome == "synced"),
        spreadsheet_id=spreadsheet_id,
        dry_run=args.dry_run,
        sync_status_text=sync_status_text,
    )

    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps([asdict(r) for r in rows], indent=2) + "\n",
            encoding="utf-8",
        )
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")

    print(md)
    return 1 if sync_outcome == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
