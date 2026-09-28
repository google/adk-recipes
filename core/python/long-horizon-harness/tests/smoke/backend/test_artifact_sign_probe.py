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

"""End-to-end signing probe (gated). Validates IAM SignBlob + the ADK blob-path
mirror: save via GcsArtifactService, sign the mirrored path, HTTP GET it.

    RUN_ARTIFACT_SIGN_PROBE=1 LOGS_BUCKET_NAME=lha-artifacts-dev \
        uv run pytest tests/smoke/backend/test_artifact_sign_probe.py -q
"""

from __future__ import annotations

import os
import urllib.request

import pytest
from google.adk.artifacts.gcs_artifact_service import GcsArtifactService
from google.genai import types as genai_types

from app.tools._artifact_links import _signed_blob_url

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_ARTIFACT_SIGN_PROBE") != "1",
    reason="set RUN_ARTIFACT_SIGN_PROBE=1 (needs LOGS_BUCKET_NAME + GCP creds)",
)


@pytest.mark.asyncio
async def test_sign_and_fetch_round_trip():
    bucket = os.environ["LOGS_BUCKET_NAME"].removeprefix("gs://")
    svc = GcsArtifactService(bucket_name=bucket)
    app, user, session, filename = (
        "horizon-probe",
        "probe-user",
        "probe-sess",
        "p.html",
    )
    payload = b"<h1>probe</h1>"

    version = await svc.save_artifact(
        app_name=app,
        user_id=user,
        session_id=session,
        filename=filename,
        artifact=genai_types.Part(
            inline_data=genai_types.Blob(data=payload, mime_type="text/html")
        ),
    )
    blob_name = f"{app}/{user}/{session}/{filename}/{version}"
    url = _signed_blob_url(bucket, blob_name, mime="text/html", inline=True)
    assert url, "signing returned None — check IAM signBlob + creds"

    with urllib.request.urlopen(url) as resp:
        assert resp.status == 200
        assert resp.read() == payload
