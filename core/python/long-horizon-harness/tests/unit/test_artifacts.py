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

"""Unit tests for the artifact(save) signed-URL surfacing."""

from __future__ import annotations

import pytest

from app.environment import LocalEnvironment
from app.environment_context import set_active_environment
from app.tools import artifacts as art


class _Actions:
    def __init__(self):
        self.artifact_delta: dict[str, int] = {}


class _Session:
    app_name = "horizon"
    user_id = "u-1"
    id = "sess-1"


class _Ctx:
    def __init__(self):
        self.state = {"id": "sess-1"}
        self.actions = _Actions()
        self.session = _Session()
        self._saved: dict[str, object] = {}

    async def save_artifact(self, *, filename, artifact):
        self._saved[filename] = artifact
        return 0


@pytest.mark.asyncio
async def test_save_returns_signed_url_when_bucket_set(tmp_path, monkeypatch):
    (tmp_path / "report.html").write_bytes(b"<h1>hi</h1>")
    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    monkeypatch.setenv("LOGS_BUCKET_NAME", "lha-artifacts-dev")
    captured = {}

    def fake_sign(bucket, blob_name, *, mime, inline):
        captured.update(blob_name=blob_name, mime=mime, inline=inline)
        return "https://storage.googleapis.com/signed"

    # `artifact_url` (imported into artifacts.py) calls `_signed_blob_url` as a
    # module global, so patch it on the source module.
    monkeypatch.setattr("app.tools._artifact_links._signed_blob_url", fake_sign)
    out = await art.artifact(
        action="save", path="report.html", tool_context=_Ctx()
    )
    assert out["success"] is True
    assert out["url"] == "https://storage.googleapis.com/signed"
    assert captured["blob_name"] == "horizon/u-1/sess-1/report.html/0"
    assert captured["mime"] == "text/html"
    assert captured["inline"] is True


@pytest.mark.asyncio
async def test_save_returns_endpoint_url_without_bucket(tmp_path, monkeypatch):
    (tmp_path / "report.html").write_bytes(b"<h1>hi</h1>")
    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)
    out = await art.artifact(
        action="save", path="report.html", tool_context=_Ctx()
    )
    assert out["url"] == "/lha/workspace/download?path=report.html&inline=1"
