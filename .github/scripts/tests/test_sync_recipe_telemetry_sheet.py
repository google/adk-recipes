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

"""Tests for .github/scripts/sync_recipe_telemetry_sheet.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_recipe_telemetry_sheet as sync  # noqa: E402


def test_classify_pull_entry_segments_agent_engine_and_pinned_tag():
    entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/"
                "python%2Fdeep-search:20261114-abc123def456"
            ),
            "authenticationInfo": {
                "principalEmail": (
                    "service-12345@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
                )
            },
            "requestMetadata": {"callerSuppliedUserAgent": "Google-Cloud-SDK"},
        }
    }
    assert sync.classify_pull_entry(entry) == (
        "python/deep-search",
        "pinned",
        "agent_engine",
    )


def test_classify_pull_entry_segments_cloud_run_and_latest_tag():
    entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/manifests/"
                "python/financial-advisor:latest"
            ),
            "authenticationInfo": {
                "principalEmail": (
                    "service-999@serverless-robot-prod.iam.gserviceaccount.com"
                )
            },
        }
    }
    assert sync.classify_pull_entry(entry) == (
        "python/financial-advisor",
        "latest",
        "cloud_run",
    )


def test_classify_pull_entry_segments_external_pull():
    entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/"
                "python/rag-agent-search@sha256:deadbeef"
            ),
            "authenticationInfo": {"principalEmail": ""},
            "requestMetadata": {"callerSuppliedUserAgent": "docker/27.0.3"},
        }
    }
    assert sync.classify_pull_entry(entry) == (
        "python/rag-agent-search",
        "latest",
        "external",
    )


def test_collect_telemetry_rows_combines_static_and_live_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    recipe_a = tmp_path / "core" / "python" / "alpha-agent"
    recipe_a.mkdir(parents=True)
    (recipe_a / "manifest.yaml").write_text(
        'language: "python"\ndeployable: true\n', encoding="utf-8"
    )
    (recipe_a / "Dockerfile").write_text(
        "FROM python:3.11-slim\n", encoding="utf-8"
    )
    (recipe_a / "telemetry.py").write_text(
        "def register_adk_telemetry():\n    pass\n", encoding="utf-8"
    )

    recipe_b = tmp_path / "contrib" / "python" / "beta-agent"
    recipe_b.mkdir(parents=True)
    (recipe_b / "manifest.yaml").write_text(
        'language: "python"\ndeployable: false\n', encoding="utf-8"
    )

    monkeypatch.setattr(
        sync,
        "fetch_registry_tags",
        lambda cat, name, tok: (3, "20261114-123456789abc"),
    )
    monkeypatch.setattr(
        sync,
        "fetch_pull_counts",
        lambda proj, days, tok: {
            "python/alpha-agent": {
                "total": 12,
                "latest": 4,
                "pinned": 8,
                "agent_engine": 7,
                "cloud_run": 3,
                "external": 2,
            }
        },
    )

    rows = sync.collect_telemetry_rows(
        tmp_path, token="fake-token", now_utc="2026-11-14T00:00:00Z"
    )
    assert len(rows) == 2

    by_name = {r.name: r for r in rows}
    alpha = by_name["alpha-agent"]
    assert alpha.deployable is True
    assert alpha.has_dockerfile is True
    assert alpha.has_gccl_telemetry is True
    assert alpha.published_tags_count == 3
    assert alpha.latest_build_tag == "20261114-123456789abc"
    assert alpha.pulls_window_total == 12
    assert alpha.pulls_agent_engine == 7
    assert alpha.pulls_cloud_run == 3
    assert alpha.pulls_external == 2

    beta = by_name["beta-agent"]
    assert beta.deployable is False
    assert beta.has_dockerfile is False
    assert beta.has_gccl_telemetry is False
    assert beta.pulls_window_total == 0


def test_sync_to_google_sheet_creates_tabs_and_writes_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, str, Any]] = []

    def fake_http_json(
        method: str,
        url: str,
        token: str | None,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> tuple[int, dict[str, Any]]:
        calls.append((method, url, payload))
        if method == "GET" and "fields=sheets.properties.title" in url:
            return 200, {"sheets": [{"properties": {"title": "Sheet1"}}]}
        return 200, {}

    monkeypatch.setattr(sync, "_http_json", fake_http_json)

    row = sync.RecipeTelemetryRow(
        snapshot_utc="2026-11-14T00:00:00Z",
        recipe_path="core/python/deep-search",
        root="core",
        category="python",
        name="deep-search",
        language="python",
        deployable=True,
        has_dockerfile=True,
        has_gccl_telemetry=True,
        gccl_token_pattern="gccl/<ver>-deep-search-<surface>-<channel>",
        published_tags_count=2,
        latest_build_tag="20261114-abcdef123456",
        pulls_window_total=5,
        pulls_latest_tag=2,
        pulls_pinned_tag=3,
        pulls_agent_engine=4,
        pulls_cloud_run=1,
        pulls_external=0,
    )

    ok = sync.sync_to_google_sheet(
        sync.DEFAULT_SPREADSHEET_ID, [row], "fake-token"
    )
    assert ok is True

    methods_and_urls = [(m, u) for m, u, _ in calls]
    # 1. GET metadata
    assert methods_and_urls[0][0] == "GET"
    # 2. POST batchUpdate to create the two missing tabs
    assert methods_and_urls[1][0] == "POST"
    assert ":batchUpdate" in methods_and_urls[1][1]
    assert len(calls[1][2]["requests"]) == 2
    # 3. POST :clear on snapshot tab
    assert ":clear" in methods_and_urls[2][1]
    # 4. PUT values on snapshot tab
    assert methods_and_urls[3][0] == "PUT"
    # 5. POST :append on history tab
    assert ":append" in methods_and_urls[4][1]


def test_sync_to_google_sheet_fails_soft_on_403(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        sync,
        "_http_json",
        lambda method, url, token, payload=None, timeout=30: (
            403,
            {"error": "permission denied"},
        ),
    )
    ok = sync.sync_to_google_sheet(
        sync.DEFAULT_SPREADSHEET_ID, [], "fake-token"
    )
    assert ok is False
