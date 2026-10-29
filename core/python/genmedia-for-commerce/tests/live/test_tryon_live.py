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

"""Live try-on runs against the real models (GENMEDIA_LIVE=1 to run).

Outputs and an events summary are saved under /tmp/genmedia_live/tryon/.
"""

import asyncio
import json
from pathlib import Path

import pytest

from genmedia4commerce.core import catalog
from genmedia4commerce.core.events import Event, VariationEvent
from genmedia4commerce.workflows.tryon.pipeline import run_tryon
from genmedia4commerce.workflows.tryon.types import TryOnRequest

pytestmark = pytest.mark.live

OUT = Path("/tmp/genmedia_live/tryon")


def _run(name: str, products: list[str], person: str, **kw) -> list[Event]:
    """Try ``products`` (catalogue refs) on ``person`` (a catalogue id)."""
    req = TryOnRequest(
        product_images=[
            catalog.image(*catalog.parse_ref(ref)) for ref in products
        ],
        person_image=catalog.image(person),
        **kw,
    )

    async def collect() -> list[Event]:
        return [event async for event in run_tryon(req)]

    events = asyncio.run(collect())
    out = OUT / name
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for event in events:
        data = event.to_dict()
        if isinstance(event, VariationEvent) and event.media:
            view = event.extra.get("view", "front")
            path = out / f"{view}_{event.index}_{event.status}.png"
            path.write_bytes(event.media)
            data["media_base64"] = str(path)
        summary.append(data)
    (out / "events.json").write_text(json.dumps(summary, indent=2))
    assert events[-1].type == "complete", summary[-1]
    assert any(
        isinstance(e, VariationEvent) and e.status == "ready" for e in events
    ), f"no publishable variation, see {out}/events.json"
    return events


def test_single_product_front_and_back():
    events = _run(
        "single_tshirt",
        ["catalog:s_genmedia_tshirt", "catalog:s_genmedia_tshirt/1"],
        "m_james",
    )
    assert events[0].payload["mode"] == "single"


def test_head_glasses():
    events = _run("head_glasses", ["catalog:p_glasses_4"], "m_emma")
    assert events[0].payload["framing"] == "head"


def test_outfit():
    garments = (
        "navy_blazer",
        "light_blue_oxford_shirt",
        "tan_chinos",
        "brown_oxford_dress_shoes",
    )
    events = _run(
        "outfit_men", [f"catalog:p_men_{g}" for g in garments], "m_james"
    )
    assert events[0].payload["mode"] == "outfit"
