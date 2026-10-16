"""Smoke test for the two media-generation models (Stage 1 image, Stage 2 video).

Run from the repo root with app/.env populated:

    set -a; source app/.env; set +a
    .venv/bin/python scripts/smoke_media_models.py            # Stage 1 + Stage 2
    .venv/bin/python scripts/smoke_media_models.py --skip-video  # Stage 1 only

Exits 0 on success. Also usable to try preview models without code changes:

    IMAGE_GENERATION_MODEL=<some-preview-id> .venv/bin/python scripts/smoke_media_models.py --skip-video
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.models.variation import CreativeVariation
from app.tools.video_tools import (
    animate_scene_with_omni,
    generate_scene_image,
)

IMAGE_MODEL = config.IMAGE_GENERATION
VIDEO_MODEL = config.VIDEO_GEN_MODEL

# Minimal product dict; prompt builders use .get() with defaults for every key.
PRODUCT = {
    "name": "sage-satin-camisole",
    "details": "A sage green satin camisole with delicate lace trim",
    "category": "summer",
    "color": "sage green",
    "fabric": "satin",
}


async def main() -> int:
    variation = CreativeVariation(name="smoke-test-studio")

    print(f"Stage 1 model: {IMAGE_MODEL}")
    scene_bytes, _ = await generate_scene_image(PRODUCT, variation)
    print(f"Stage 1 OK: {len(scene_bytes)} bytes")

    if "--skip-video" in sys.argv:
        print("Stage 2 skipped (--skip-video)")
        return 0

    print(f"Stage 2 model: {VIDEO_MODEL}")
    video_bytes, _ = await animate_scene_with_omni(
        scene_bytes, PRODUCT, variation, duration_seconds=4
    )
    print(f"Stage 2 OK: {len(video_bytes)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
