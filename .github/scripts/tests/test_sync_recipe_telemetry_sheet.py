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
        "pinned",
        "external",
    )


def test_classify_pull_entry_ignores_ci_trivy_and_handles_get_tag():
    ci_sa_entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/"
                "python/rag-agent-search:20261114-abc123def456"
            ),
            "authenticationInfo": {
                "principalEmail": sync.DEFAULT_SERVICE_ACCOUNT
            },
            "requestMetadata": {"callerSuppliedUserAgent": "docker/27.0.3"},
        }
    }
    assert sync.classify_pull_entry(ci_sa_entry) is None

    trivy_entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/"
                "python/rag-agent-search:20261114-abc123def456"
            ),
            "authenticationInfo": {"principalEmail": ""},
            "requestMetadata": {"callerSuppliedUserAgent": "trivy/0.36.0"},
        }
    }
    assert sync.classify_pull_entry(trivy_entry) is None

    custom_sa_entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/"
                "python/rag-agent-search:20261114-abc123def456"
            ),
            "authenticationInfo": {
                "principalEmail": "custom-ci@example.iam.gserviceaccount.com"
            },
            "requestMetadata": {"callerSuppliedUserAgent": "docker/27.0.3"},
        }
    }
    assert (
        sync.classify_pull_entry(
            custom_sa_entry,
            service_account="custom-ci@example.iam.gserviceaccount.com",
        )
        is None
    )

    get_tag_entry = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/packages/"
                "python%2Fdeep-search/tags/latest"
            ),
            "authenticationInfo": {"principalEmail": ""},
            "requestMetadata": {"callerSuppliedUserAgent": "docker/27.0.3"},
        }
    }
    assert sync.classify_pull_entry(get_tag_entry) == (
        "python/deep-search",
        "latest",
        "external",
    )


def test_classify_pull_entry_uses_repo_name_from_registry(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        sync, "REGISTRY", "us-central1-docker.pkg.dev/other-proj/custom-repo"
    )
    entry = {
        "protoPayload": {
            "resourceName": (
                "projects/other-proj/locations/us-central1/"
                "repositories/custom-repo/packages/"
                "python%2Fdeep-search/tags/20261114-abc123def456"
            ),
            "authenticationInfo": {"principalEmail": ""},
            "requestMetadata": {"callerSuppliedUserAgent": "docker/27.0.3"},
        }
    }
    assert sync.classify_pull_entry(entry) == (
        "python/deep-search",
        "pinned",
        "external",
    )


def test_fetch_registry_tags_respects_project_id_override(
    monkeypatch: pytest.MonkeyPatch,
):
    seen_urls: list[str] = []

    def fake_http_json(
        method: str,
        url: str,
        token: str | None,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
        *,
        idempotent: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        seen_urls.append(url)
        return 200, {
            "tags": [
                {
                    "name": (
                        "projects/custom-proj/locations/us-west1/repositories/"
                        "adk-recipes-registry/packages/python%2Falpha/tags/"
                        "20261114-abcdef123456"
                    )
                }
            ]
        }

    monkeypatch.setattr(sync, "_http_json", fake_http_json)
    count, latest = sync.fetch_registry_tags(
        "python", "alpha", "fake-token", project_id="custom-proj"
    )
    assert (count, latest) == (1, "20261114-abcdef123456")
    assert seen_urls
    assert "/v1/projects/custom-proj/" in seen_urls[0]


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

    seen_tag_project_ids: list[str | None] = []
    seen_pull_service_accounts: list[str] = []

    def fake_fetch_registry_tags(
        cat: str,
        name: str,
        tok: str | None,
        *,
        project_id: str | None = None,
    ) -> tuple[int, str]:
        seen_tag_project_ids.append(project_id)
        return (3, "20261114-123456789abc")

    def fake_fetch_pull_counts(
        proj: str,
        days: int,
        tok: str | None,
        *,
        service_account: str = sync.DEFAULT_SERVICE_ACCOUNT,
    ) -> dict[str, dict[str, int]]:
        seen_pull_service_accounts.append(service_account)
        return {
            "python/alpha-agent": {
                "total": 12,
                "latest": 4,
                "pinned": 8,
                "agent_engine": 7,
                "cloud_run": 3,
                "external": 2,
            }
        }

    monkeypatch.setattr(sync, "fetch_registry_tags", fake_fetch_registry_tags)
    monkeypatch.setattr(sync, "fetch_pull_counts", fake_fetch_pull_counts)

    rows = sync.collect_telemetry_rows(
        tmp_path,
        project_id="my-custom-proj",
        token="fake-token",
        now_utc="2026-11-14T00:00:00Z",
        service_account="custom-sa@example.iam.gserviceaccount.com",
    )
    assert len(rows) == 2
    assert seen_tag_project_ids == ["my-custom-proj"]
    assert seen_pull_service_accounts == [
        "custom-sa@example.iam.gserviceaccount.com"
    ]

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
        *,
        idempotent: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        calls.append((method, url, payload))
        if method == "GET" and "fields=sheets.properties" in url:
            return 200, {
                "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]
            }
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

    assert len(calls) == 5
    # 1. GET metadata
    assert calls[0][0] == "GET"
    # 2. POST batchUpdate to create the two missing tabs
    assert calls[1][0] == "POST"
    assert ":batchUpdate" in calls[1][1]
    assert len(calls[1][2]["requests"]) == 2
    created_titles = [
        r["addSheet"]["properties"]["title"] for r in calls[1][2]["requests"]
    ]
    assert created_titles == [
        sync.DEFAULT_SNAPSHOT_TAB,
        sync.DEFAULT_HISTORY_TAB,
    ]
    # 3. PUT values on snapshot tab first (before clearing trailing rows)
    assert calls[2][0] == "PUT"
    assert "%27Recipe%20Telemetry%27%21A1" in calls[2][1]
    assert calls[2][2] == {"values": [sync.SHEET_HEADERS, row.to_sheet_row()]}
    # 4. POST :clear on trailing rows of snapshot tab
    assert calls[3][0] == "POST"
    assert "%27Recipe%20Telemetry%27%21A3%3AR:clear" in calls[3][1]
    # 5. POST :append on history tab (includes header row because newly created)
    assert calls[4][0] == "POST"
    assert "%27Telemetry%20Log%27%21A1:append" in calls[4][1]
    assert calls[4][2] == {"values": [sync.SHEET_HEADERS, row.to_sheet_row()]}


def test_sync_to_google_sheet_returns_false_on_clear_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_http_json(
        method: str,
        url: str,
        token: str | None,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
        *,
        idempotent: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and "fields=sheets.properties" in url:
            return 200, {
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 0,
                            "title": sync.DEFAULT_SNAPSHOT_TAB,
                        }
                    },
                    {
                        "properties": {
                            "sheetId": 1,
                            "title": sync.DEFAULT_HISTORY_TAB,
                        }
                    },
                ]
            }
        if ":clear" in url:
            return 500, {"error": "internal error"}
        return 200, {}

    monkeypatch.setattr(sync, "_http_json", fake_http_json)
    ok = sync.sync_to_google_sheet(
        sync.DEFAULT_SPREADSHEET_ID, [], "fake-token"
    )
    assert ok is False


def test_sync_to_google_sheet_fails_soft_on_403(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        sync,
        "_http_json",
        lambda method, url, token, payload=None, timeout=30, **kwargs: (
            403,
            {"error": "permission denied"},
        ),
    )
    ok = sync.sync_to_google_sheet(
        sync.DEFAULT_SPREADSHEET_ID, [], "fake-token"
    )
    assert ok is False
    assert (
        sync.sync_to_google_sheet_with_status(
            sync.DEFAULT_SPREADSHEET_ID, [], "fake-token"
        )
        == "permission_denied"
    )


def test_classify_pull_entry_no_backtracking_and_checks_request_for_tag():
    # Single-segment image or non-image collection paths must not backtrack.
    bad_single = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/single-name:latest"
            )
        }
    }
    assert sync.classify_pull_entry(bad_single) is None

    bad_files = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/files/python%2Fdeep-search"
            )
        }
    }
    assert sync.classify_pull_entry(bad_files) is None

    # Untagged resourceName should still inspect request/metadata for pinned ref.
    split_ref = {
        "protoPayload": {
            "resourceName": (
                "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                "repositories/adk-recipes-registry/dockerImages/"
                "python%2Fdeep-search"
            ),
            "request": {
                "name": (
                    "projects/adk-samples-repo-gcp-support/locations/us-west1/"
                    "repositories/adk-recipes-registry/dockerImages/"
                    "python%2Fdeep-search:20261114-abc123def456"
                )
            },
            "authenticationInfo": {"principalEmail": ""},
        }
    }
    assert sync.classify_pull_entry(split_ref) == (
        "python/deep-search",
        "pinned",
        "external",
    )


def test_sync_skips_clear_when_sheet_has_no_trailing_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, str, Any]] = []

    def fake_http_json(
        method: str,
        url: str,
        token: str | None,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
        *,
        idempotent: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        calls.append((method, url, payload))
        if method == "GET" and "fields=sheets.properties" in url:
            return 200, {
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 567149855,
                            "title": "Team's Telemetry",
                            "gridProperties": {
                                "rowCount": 2,
                                "columnCount": 18,
                            },
                        }
                    },
                    {
                        "properties": {
                            "sheetId": 2,
                            "title": sync.DEFAULT_HISTORY_TAB,
                            "gridProperties": {
                                "rowCount": 100,
                                "columnCount": 18,
                            },
                        }
                    },
                ]
            }
        if method == "GET" and "A1%3AA1" in url:
            # Existing history tab is empty -> headers must be written.
            return 200, {}
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
        published_tags_count=1,
        latest_build_tag="20261114-abcdef123456",
        pulls_window_total=0,
        pulls_latest_tag=0,
        pulls_pinned_tag=0,
        pulls_agent_engine=0,
        pulls_cloud_run=0,
        pulls_external=0,
    )

    ok = sync.sync_to_google_sheet(
        sync.DEFAULT_SPREADSHEET_ID,
        [row],
        "fake-token",
        sheet_gid="567149855",
    )
    assert ok is True
    # No :clear call when len(values) == 2 == rowCount.
    assert not any(":clear" in url for _, url, _ in calls)
    # Single quote in tab title escaped as '' in A1 notation.
    put_call = next(c for c in calls if c[0] == "PUT")
    assert "%27Team%27%27s%20Telemetry%27%21A1" in put_call[1]
    # Existing empty history tab receives headers + row.
    append_call = next(c for c in calls if ":append" in c[1])
    assert append_call[2] == {
        "values": [sync.SHEET_HEADERS, row.to_sheet_row()]
    }


def test_sync_rejects_tab_collision_and_invalid_ids(
    monkeypatch: pytest.MonkeyPatch,
):
    assert (
        sync.sync_to_google_sheet_with_status(
            "bad/sheet?id=1", [], "fake-token"
        )
        == "error"
    )

    def fake_http_json(
        method: str,
        url: str,
        token: str | None,
        payload: dict[str, Any] | None = None,
        timeout: int = 30,
        *,
        idempotent: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        return 200, {
            "sheets": [
                {
                    "properties": {
                        "sheetId": 99,
                        "title": sync.DEFAULT_HISTORY_TAB,
                    }
                }
            ]
        }

    monkeypatch.setattr(sync, "_http_json", fake_http_json)
    assert (
        sync.sync_to_google_sheet_with_status(
            sync.DEFAULT_SPREADSHEET_ID,
            [],
            "fake-token",
            sheet_gid="99",
        )
        == "error"
    )


def test_http_json_does_not_retry_non_idempotent_post(
    monkeypatch: pytest.MonkeyPatch,
):
    import urllib.error

    attempts = 0

    def fake_urlopen(req, timeout=30):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, None
        )

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sync.time, "sleep", lambda _s: None)

    status, _ = sync._http_json(
        "POST", "https://example.test/append", "tok", {}
    )
    assert status == 503
    assert attempts == 1

    attempts = 0
    status, _ = sync._http_json(
        "POST", "https://example.test/clear", "tok", {}, idempotent=True
    )
    assert status == 503
    assert attempts == 4


def test_col_letter_and_project_id_validation():
    assert sync._col_letter(1) == "A"
    assert sync._col_letter(18) == "R"
    assert sync._col_letter(26) == "Z"
    assert sync._col_letter(27) == "AA"
    with pytest.raises(ValueError):
        sync._validate_project_id("bad/project")
    with pytest.raises(ValueError):
        sync._validate_spreadsheet_id("../escape")


def test_main_exits_nonzero_on_sheet_error_and_zero_on_403(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(sync, "collect_telemetry_rows", lambda *a, **kw: [])

    monkeypatch.setattr(
        sync, "sync_to_google_sheet_with_status", lambda *a, **kw: "error"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_recipe_telemetry_sheet.py",
            "--access-token",
            "tok",
            "--out-md",
            str(tmp_path / "err.md"),
        ],
    )
    assert sync.main() == 1
    assert "Failed (see workflow logs)" in (tmp_path / "err.md").read_text(
        encoding="utf-8"
    )

    monkeypatch.setattr(
        sync,
        "sync_to_google_sheet_with_status",
        lambda *a, **kw: "permission_denied",
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_recipe_telemetry_sheet.py",
            "--access-token",
            "tok",
            "--out-md",
            str(tmp_path / "perm.md"),
        ],
    )
    capsys.readouterr()
    assert sync.main() == 0
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "permission denied" in (tmp_path / "perm.md").read_text(
        encoding="utf-8"
    )


def test_fetch_pull_counts_warns_with_private_log_viewer_role(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr(
        sync,
        "_http_json",
        lambda *a, **kw: (403, {"error": {"message": "Permission denied"}}),
    )
    counts = sync.fetch_pull_counts(
        "adk-samples-repo-gcp-support",
        30,
        "fake-token",
        service_account="custom-sa@example.iam.gserviceaccount.com",
    )
    assert counts == {}
    err = capsys.readouterr().err
    assert "roles/logging.privateLogViewer" in err
    assert "DATA_READ" in err
    assert "custom-sa@example.iam.gserviceaccount.com" in err
