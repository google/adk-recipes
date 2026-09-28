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

"""Unit tests for the artifact link builder."""

from __future__ import annotations

from app.tools import _artifact_links as al


def test_workspace_download_url_is_root_relative_inline():
    assert (
        al.workspace_download_url("report.html")
        == "/lha/workspace/download?path=report.html&inline=1"
    )


def test_workspace_download_url_attachment_drops_inline():
    assert (
        al.workspace_download_url("r.html", inline=False)
        == "/lha/workspace/download?path=r.html"
    )


def test_workspace_download_url_encodes_special_chars():
    assert "path=my%20report.html" in al.workspace_download_url(
        "my report.html", inline=False
    )


def test_artifact_url_falls_back_to_endpoint_without_bucket(monkeypatch):
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)
    url = al.artifact_url(
        app_name="horizon",
        user_id="u1",
        session_id="s1",
        filename="plot.html",
        version=0,
        rel_path="plot.html",
        mime="text/html",
        inline=True,
    )
    assert url == "/lha/workspace/download?path=plot.html&inline=1"


def test_artifact_url_signs_blob_when_bucket_set(monkeypatch):
    monkeypatch.setenv("LOGS_BUCKET_NAME", "lha-artifacts-dev")
    captured = {}

    def fake_sign(bucket, blob_name, *, mime, inline):
        captured.update(
            bucket=bucket, blob_name=blob_name, mime=mime, inline=inline
        )
        return "https://storage.googleapis.com/signed"

    monkeypatch.setattr(al, "_signed_blob_url", fake_sign)
    url = al.artifact_url(
        app_name="horizon",
        user_id="u1",
        session_id="s1",
        filename="plot.html",
        version=3,
        rel_path="sub/plot.html",
        mime="text/html",
        inline=True,
    )
    assert url == "https://storage.googleapis.com/signed"
    assert captured["blob_name"] == "horizon/u1/s1/plot.html/3"
    assert captured["mime"] == "text/html"
    assert captured["inline"] is True


def test_artifact_url_forwards_inline_false_to_signer(monkeypatch):
    monkeypatch.setenv("LOGS_BUCKET_NAME", "lha-artifacts-dev")
    captured = {}

    def fake_sign(bucket, blob_name, *, mime, inline):
        captured.update(inline=inline)
        return "https://storage.googleapis.com/signed"

    monkeypatch.setattr(al, "_signed_blob_url", fake_sign)
    al.artifact_url(
        app_name="horizon",
        user_id="u1",
        session_id="s1",
        filename="f",
        version=0,
        rel_path="f",
        mime="text/plain",
        inline=False,
    )
    assert captured["inline"] is False


def test_artifact_url_falls_back_when_signing_fails(monkeypatch):
    monkeypatch.setenv("LOGS_BUCKET_NAME", "lha-artifacts-dev")
    monkeypatch.setattr(al, "_signed_blob_url", lambda *a, **k: None)
    url = al.artifact_url(
        app_name="horizon",
        user_id="u1",
        session_id="s1",
        filename="plot.html",
        version=0,
        rel_path="plot.html",
        mime="text/html",
        inline=False,
    )
    assert url == "/lha/workspace/download?path=plot.html"
