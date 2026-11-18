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

"""Live REST check: one small try-on through the real app (GENMEDIA_LIVE=1)."""

import json

import pytest
from fastapi.testclient import TestClient

from genmedia4commerce.api.app import create_app
from genmedia4commerce.core import catalog

pytestmark = pytest.mark.live


def test_tryon_streams_to_complete():
    product = catalog.image("p_men_navy_polo_shirt")
    person = catalog.image("m_luca")
    client = TestClient(create_app())

    with client.stream(
        "POST",
        "/api/tryon",
        files=[
            ("product_images", ("polo.webp", product, "image/webp")),
            ("person_image", ("luca.webp", person, "image/webp")),
        ],
        data={"num_variations": "1", "image_size": "1K"},
        timeout=900,
    ) as response:
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]

    assert (events[0]["type"], events[0]["stage"]) == ("progress", "run")
    assert events[1]["type"] == "classified"
    assert events[-1]["type"] == "complete"
