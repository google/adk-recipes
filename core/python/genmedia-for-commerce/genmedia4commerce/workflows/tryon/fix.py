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

"""One fix pass for a judge-rejected try-on image (``GENMEDIA_TRYON_FIX``).

``fixing`` wraps a drawing's evaluate: a rejection (never a judge error)
gets one fix attempt, the request of ``fix_prompt`` drawn with the
drawing's params within the run deadline, and the fixed image is judged
again; a fixed image is never fixed again. A published fix replaces the
image and its verdict (``details["fixed"] = True``); otherwise the image
and verdict are kept, with the attempt in ``details["fix"]``:
``{"published": False, "description"}`` (the fix's verdict),
``{"skipped": "image limit"}`` (more images than ``gen.MAX_IMAGES``:
nothing is dropped) or ``{"error"}`` (a failed call).
"""

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable

from genmedia4commerce.core import gen, trace
from genmedia4commerce.core.errors import user_message
from genmedia4commerce.core.gen import ImageParams, Part
from genmedia4commerce.core.runner import Evaluate
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.workflows.tryon import fix_prompt
from genmedia4commerce.workflows.tryon.types import PromptSpec

__all__ = ["fixing"]

logger = logging.getLogger(__name__)

Within = Callable[[Awaitable], Awaitable]


def _noted(verdict: Verdict, **details) -> Verdict:
    """``verdict`` with ``details`` added."""
    return dataclasses.replace(verdict, details={**verdict.details, **details})


def fixing(
    spec: PromptSpec,
    parts: list[Part],
    params: ImageParams,
    within: Within,
    evaluate: Evaluate,
) -> Evaluate:
    """``evaluate`` (bounded by the caller) with one fix pass on a
    rejection; ``parts``: the drawing's request, ``within`` bounds the
    fix's image call with the run deadline."""

    async def attempt(verdict: Verdict, image: bytes) -> tuple[Verdict, bytes]:
        """One attempt: the fixed image and its verdict when published,
        else ``image`` and ``verdict`` with the attempt noted."""
        system, request = fix_prompt.build_prompt(spec, parts, image, verdict)
        count = sum(not isinstance(p, str) for p in request)
        if count > gen.MAX_IMAGES:
            logger.warning(
                f"try-on fix skipped: {count} images, the image model "
                f"takes {gen.MAX_IMAGES}"
            )
            return _noted(verdict, fix={"skipped": "image limit"}), image
        with trace.scope(fix=1):
            fixed = await within(
                asyncio.to_thread(
                    gen.generate_image, request, system=system, params=params
                )
            )
            again, fixed = await evaluate(fixed)
            trace.save("fix", again)
        if again.publish and not again.error:
            return _noted(again, fixed=True), fixed
        tried = {"published": False, "description": again.description}
        return _noted(verdict, fix=tried), image

    async def judge(image: bytes) -> tuple[Verdict, bytes]:
        verdict, image = await evaluate(image)
        if verdict.publish or verdict.error:
            return verdict, image
        try:
            return await attempt(verdict, image)
        except Exception as exc:
            logger.warning(f"try-on fix failed, the image is kept: {exc}")
            return _noted(verdict, fix={"error": user_message(exc)}), image

    return judge
