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

"""App fixtures: no ADC, fake workflows, a temporary frontend dist."""

import json

import google.auth
import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import DefaultCredentialsError

from genmedia4commerce.core.events import CompleteEvent, VariationEvent
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Verdict


def _no_adc(*_args, **_kwargs):
    raise DefaultCredentialsError("no ADC in unit tests")


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    """The ADK app probes ADC for Cloud telemetry; unit tests have none."""
    monkeypatch.setattr(google.auth, "default", _no_adc)


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """A fake ``frontend/dist`` with index.html and one bundled asset."""
    from genmedia4commerce.api import app as app_module

    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>spa</html>")
    (root / "assets" / "index.js").write_text("js")
    monkeypatch.setattr(app_module, "DIST_DIR", root)
    return root


@pytest.fixture
def client(dist):
    """Test client of a fresh app (lifespan not started)."""
    from genmedia4commerce.api.app import create_app

    return TestClient(create_app())


IMAGE = png(8, 8, (9, 9, 9))
EVENTS = [
    VariationEvent(
        index=0,
        status="ready",
        media=IMAGE,
        verdict=Verdict(publish=True),
        score=1.0,
    ),
    CompleteEvent(ranked_indices=[0], best_index=0),
]


def fake_workflow(*events):
    """Async-generator workflow that records its requests."""
    requests = []

    async def run(req):
        requests.append(req)
        for event in events or EVENTS:
            yield event

    run.requests = requests
    return run


def frames(text: str) -> list[dict]:
    """Parse the ``data:`` frames of an SSE body."""
    return [
        json.loads(chunk.removeprefix("data: "))
        for chunk in text.split("\n\n")
        if chunk.startswith("data: ")
    ]
