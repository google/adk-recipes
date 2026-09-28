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

"""URI-selection rules for ADK's session_service backend.

Scope rule: "ADK InMemorySessionService for prototype; upgrade
path to DatabaseSessionService / VertexAiSessionService is config-only."

These tests exercise the URI-selection logic in isolation, without booting
the FastAPI app or touching Vertex / Agent Engine.
"""

from __future__ import annotations

import logging
import os

# Skip the module-level FastAPI build (which calls google.auth.default() and
# hits Agent Engine) before importing the module — these tests only exercise
# the pure URI-selection helper.
os.environ["LHA_ADK_SKIP_APP_BUILD"] = "true"

import pytest

from app.fast_api_app import (
    _resolve_service_uris,
    resolve_session_service_uri,
)


def test_in_memory_when_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "true")
    monkeypatch.setenv("SESSION_DB_URL", "sqlite:///./should-be-ignored.db")
    monkeypatch.setenv("AGENT_ENGINE_SESSION_NAME", "ignored-engine")

    assert resolve_session_service_uri() is None


def test_returns_none_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_IN_MEMORY_SESSION", raising=False)
    monkeypatch.delenv("SESSION_DB_URL", raising=False)
    monkeypatch.delenv("AGENT_ENGINE_SESSION_NAME", raising=False)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)

    # No flags set => caller is responsible for falling back to Agent Engine
    # discovery; the helper signals "no DB-shaped override" with None.
    assert resolve_session_service_uri() is None


def test_returns_session_db_url_when_only_db_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USE_IN_MEMORY_SESSION", raising=False)
    monkeypatch.setenv("SESSION_DB_URL", "sqlite:///./sessions.db")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)

    assert resolve_session_service_uri() == "sqlite:///./sessions.db"


def test_returns_session_db_url_for_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USE_IN_MEMORY_SESSION", raising=False)
    monkeypatch.setenv("SESSION_DB_URL", "postgresql://user:pass@host:5432/db")

    assert (
        resolve_session_service_uri() == "postgresql://user:pass@host:5432/db"
    )


def test_db_url_wins_over_agent_engine_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("USE_IN_MEMORY_SESSION", raising=False)
    monkeypatch.setenv("SESSION_DB_URL", "sqlite:///./sessions.db")
    monkeypatch.setenv(
        "AGENT_ENGINE_RESOURCE_NAME",
        "projects/p/locations/l/reasoningEngines/123",
    )

    with caplog.at_level(logging.WARNING, logger="app.fast_api_app"):
        result = resolve_session_service_uri()

    assert result == "sqlite:///./sessions.db"
    assert any(
        "SESSION_DB_URL" in rec.message and "AGENT_ENGINE" in rec.message
        for rec in caplog.records
    ), (
        f"expected precedence warning, got: {[r.message for r in caplog.records]}"
    )


def test_no_warning_when_only_one_source_set(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("USE_IN_MEMORY_SESSION", raising=False)
    monkeypatch.setenv("SESSION_DB_URL", "sqlite:///./sessions.db")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("AGENT_ENGINE_SESSION_NAME", raising=False)

    with caplog.at_level(logging.WARNING, logger="app.fast_api_app"):
        resolve_session_service_uri()

    assert not any("SESSION_DB_URL" in rec.message for rec in caplog.records), (
        "no precedence warning should fire when only SESSION_DB_URL is set"
    )


def test_default_session_sqlite_lives_under_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no explicit URL / in-memory / deploy flags, the dev default sqlite
    # path is relocated under .data/ to keep the repo root clean.
    monkeypatch.delenv("USE_IN_MEMORY_SESSION", raising=False)
    monkeypatch.delenv("SESSION_DB_URL", raising=False)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("LHA_MEMORY_BANK_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)

    session_uri, _memory_uri, _artifact_uri = _resolve_service_uris()

    assert session_uri is not None
    assert "/.data/lha_sessions.db" in session_uri
    assert session_uri.startswith("sqlite:///")


def _stub_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin an explicit session URL so _resolve_service_uris never reaches the
    # Vertex Agent Engine branch.
    monkeypatch.setenv("SESSION_DB_URL", "sqlite:///./x.db")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)


def test_artifact_uri_derived_from_logs_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session(monkeypatch)
    monkeypatch.delenv("ARTIFACT_SERVICE_URI", raising=False)
    monkeypatch.setenv("LOGS_BUCKET_NAME", "my-bucket")

    _s, _m, artifact_uri = _resolve_service_uris()
    assert artifact_uri == "gs://my-bucket"


def test_artifact_service_uri_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session(monkeypatch)
    monkeypatch.setenv("ARTIFACT_SERVICE_URI", "gs://explicit-bucket/prefix")
    monkeypatch.setenv("LOGS_BUCKET_NAME", "ignored-bucket")

    _s, _m, artifact_uri = _resolve_service_uris()
    assert artifact_uri == "gs://explicit-bucket/prefix"


def test_artifact_uri_none_without_bucket_or_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session(monkeypatch)
    monkeypatch.delenv("ARTIFACT_SERVICE_URI", raising=False)
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)

    _s, _m, artifact_uri = _resolve_service_uris()
    assert artifact_uri is None
