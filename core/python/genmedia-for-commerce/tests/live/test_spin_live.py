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

"""Live 360 spin runs (real models). ``GENMEDIA_LIVE=1`` to run.

Spins (animated WebP) and verdicts go to ``/tmp/genmedia_live/spin/``;
label the clips and add them to the rotation benchmark
(``tests/benchmark/rotation``).
"""

import json
from pathlib import Path

import pytest

from genmedia4commerce.core import catalog
from genmedia4commerce.core.events import VariationEvent
from genmedia4commerce.workflows.spin.pipeline import run_spin
from genmedia4commerce.workflows.spin.types import SpinRequest

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

OUT = Path("/tmp/genmedia_live/spin")
SHOES = "s_aura_running_shoe"  # spin sets of the catalogue
PHONE = "s_smartphone"


def _images(item_id: str) -> list[bytes]:
    count = catalog.item(item_id)["images"]
    return [catalog.image(item_id, n) for n in range(count)]


async def _run(name: str, images: list[bytes]) -> dict:
    """Run the spin, save every variation video and the event log."""
    OUT.mkdir(parents=True, exist_ok=True)
    log = []
    async for event in run_spin(SpinRequest(images)):
        if isinstance(event, VariationEvent) and event.media:
            (OUT / f"{name}_{event.index}_{event.status}.webp").write_bytes(
                event.media
            )
        record = event.to_dict()
        record.pop("media_base64", None)
        log.append(record)
    (OUT / f"{name}_events.json").write_text(json.dumps(log, indent=2))
    assert log[-1]["type"] == "complete", log[-1]
    return {
        "classified": log[0],
        "statuses": [e["status"] for e in log if e["type"] == "variation"],
    }


async def test_shoes_spin():
    result = await _run("shoes", _images(SHOES))
    assert result["classified"]["payload"]["product_type"] == "shoes"
    assert "ready" in result["statuses"], result


async def test_generic_spin():
    result = await _run("phone", _images(PHONE))
    assert result["classified"]["payload"]["product_type"] == "other"
    assert len(result["statuses"]) >= 1
