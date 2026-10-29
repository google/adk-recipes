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

"""A REST run is traced like a tool run, although it streams in another task."""

import asyncio
import json

from genmedia4commerce.api import tryon
from genmedia4commerce.core import trace
from tests.api.conftest import EVENTS, IMAGE, frames
from tests.conftest import trace_file


def test_tryon_route_traces_the_run(client, monkeypatch, debug_dir):
    async def run(req):
        trace.save("in-workflow", "step")
        await asyncio.to_thread(trace.save, "in-thread", b"\x89PNG\r\n\x1a\n")
        for event in EVENTS:
            yield event

    run.__name__ = "run_tryon"
    monkeypatch.setattr(tryon, "run_tryon", run)
    response = client.post(
        "/api/tryon",
        files=[
            ("product_images", ("p.png", IMAGE, "image/png")),
            ("person_image", ("q.png", IMAGE, "image/png")),
        ],
        data={"session_id": "sess-1"},
    )
    run_id = frames(response.text)[0]["data"]["run_id"]
    (root,) = debug_dir.glob(f"*/{run_id}")
    info = json.loads((root / "run.json").read_text())
    assert (info["workflow"], info["session_id"]) == ("tryon", "sess-1")
    assert trace_file(root, "_request.json")
    assert trace_file(root, "_inputs-person_image-0.png").read_bytes() == IMAGE
    assert trace_file(root, "_in-workflow.txt").read_text() == "step"
    assert trace_file(root, "_in-thread.png")
