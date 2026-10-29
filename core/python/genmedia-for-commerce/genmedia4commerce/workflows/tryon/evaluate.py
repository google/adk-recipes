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

"""Score one try-on image: one outfit judge call.

Every product drawn is shown to the judge with the image the drawing got
for it (its card without the close-ups: its chosen upload, or its crop of
the look photo for a product the look added), and the judge decides on the
whole image in one call (``eval.outfit``). A
product the styling plan wears under other garments is judged on the part
meant to be visible (its plan ``visibility``). On the front view the judge
also sees the wearer as the drawing got it (``ctx.wearer_body``) and the
face crop of the original photo (``ctx.reference_face``, when it has a
face), and rejects a body, prosthesis or aid added, removed or reshaped,
a face that is not the wearer's, or a bare chest or underwear on show;
the back view, drawn without them, is judged without them.
"""

import asyncio
import dataclasses
from collections.abc import Sequence

from genmedia4commerce.core.types import Product, Verdict, quality_score
from genmedia4commerce.eval import outfit as outfit_eval
from genmedia4commerce.workflows.tryon.types import (
    PromptProduct,
    TryOnContext,
    ViewName,
)

__all__ = ["evaluator"]


def _eval_refs(product: Product, view: ViewName) -> list[bytes]:
    """Return the best upload of ``view`` (else the best upload); no crops
    (the video try-on's evidence)."""
    uploads = dataclasses.replace(
        product, refs=[r for r in product.refs if r.source == "upload"]
    )
    best = uploads.best_ref(view) or uploads.best_ref()
    return [best.image] if best else []


def _visibility(ctx: TryOnContext, product_id: str) -> str | None:
    """Return the plan's visibility of a partly covered product, else None."""
    items = ctx.plan.items if ctx.plan else []
    item = next((i for i in items if i.get("product_id") == product_id), {})
    text = str(item.get("visibility") or "").strip()
    if not text or text.lower().startswith("fully visible"):
        return None
    return text


def _combine(products: Sequence[PromptProduct], verdict: Verdict) -> Verdict:
    judged = {d["index"]: d for d in verdict.details.get("products", [])}
    return Verdict(
        publish=verdict.publish,
        imperfection_count=verdict.imperfection_count,
        description=verdict.description,
        error=verdict.error,
        details={
            "products": [
                {
                    "product_id": p.id,
                    "defects": judged.get(k, {}).get("defects", []),
                    "findings": judged.get(k, {}).get("findings", []),
                }
                for k, p in enumerate(products, start=1)
            ],
            "body_defects": verdict.details.get("body_defects", []),
            "body_parts": verdict.details.get("body_parts", []),
            "score": quality_score(verdict.publish, verdict.imperfection_count),
        },
    )


def evaluator(
    ctx: TryOnContext, view: ViewName, products: Sequence[PromptProduct]
):
    """Return ``evaluate(image) -> (verdict, image)`` for the runner;
    ``products``: the drawing's, each judged on its first image.

    The caller bounds each call with the run deadline.
    """
    products = [p for p in products if p.refs]
    items = [
        outfit_eval.OutfitProduct(
            p.refs[0].image, p.category, _visibility(ctx, p.id)
        )
        for p in products
    ]
    front = view == "front"
    wearer = ctx.wearer_body if front else None
    face = ctx.reference_face if front else None

    async def evaluate(image: bytes) -> tuple[Verdict, bytes]:
        if not items:
            return _combine(products, Verdict(publish=True)), image
        verdict = await asyncio.to_thread(
            outfit_eval.evaluate_outfit, items, image, wearer=wearer, face=face
        )
        return _combine(products, verdict), image

    return evaluate
