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

"""``run_tryon``: classify, prepare, generate N variations, eval, rank, stream.

Events: classified, progress("preprocessed"), record (``record.record``:
what the images are made from, for a later video of one of them),
variation(view=front)*, then
for a single product with back refs progress("back") and
variation(view=back)* (of the best front; ``back_view`` draws the back of
any front), then exactly one complete (front ranking). Any failure outside
the runner ends the run with one error event.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncGenerator, Awaitable

from genmedia4commerce.core import degraded, gen, trace
from genmedia4commerce.core.errors import (
    DeadlineExceeded,
    InputError,
    user_message,
)
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    Event,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.retry import Deadline
from genmedia4commerce.core.runner import Evaluate, RunPolicy, run_variations
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import (
    Classification,
    Product,
    ProductRef,
    Verdict,
)
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import (
    closeups,
    fix,
    inputs,
    outfit,
    record,
    reference,
    worn,
)
from genmedia4commerce.workflows.tryon.cache import Analysis, TryOnCache
from genmedia4commerce.workflows.tryon.classify import classify_products
from genmedia4commerce.workflows.tryon.evaluate import evaluator
from genmedia4commerce.workflows.tryon.prompt import (
    PromptProduct,
    PromptSpec,
    build_prompt,
)
from genmedia4commerce.workflows.tryon.types import (
    Prepared,
    TryOnContext,
    TryOnRequest,
    ViewName,
)

__all__ = ["MAX_PRODUCT_IMAGES", "back_view", "run_tryon"]

logger = logging.getLogger(__name__)

MAX_PRODUCT_IMAGES = 20


def _validate(req: TryOnRequest) -> None:
    n = len(req.product_images)
    if not 1 <= n <= MAX_PRODUCT_IMAGES:
        raise InputError(
            f"1 to {MAX_PRODUCT_IMAGES} product images required, got {n}"
        )
    if not all(req.product_images):
        raise InputError("product images must not be empty")
    if not req.person_image:
        raise InputError("person_image must not be empty")


async def _within(deadline: Deadline, awaitable: Awaitable):
    try:
        return await asyncio.wait_for(awaitable, timeout=deadline.remaining)
    except TimeoutError as exc:
        raise DeadlineExceeded(
            f"try-on exceeded {deadline.seconds:g}s"
        ) from exc


async def _plan(
    store: TryOnCache,
    classification: Classification,
    chosen: list[tuple[Product, ProductRef]],
    guidelines: str,
):
    """An outfit's styling plan (kept or made); None for one product."""
    if classification.mode != "outfit":
        return None
    shown = [r.image for _, r in chosen]
    crops = outfit.extra_crops(classification.products, shown)
    return await store.plan(
        guidelines,
        lambda: asyncio.to_thread(
            outfit.analyze_outfit, chosen, crops, guidelines
        ),
    )


async def _prepare(
    req: TryOnRequest,
    classification: Classification,
    chosen: list[tuple[Product, ProductRef]],
    store: TryOnCache,
) -> TryOnContext:
    """Wearer prep and (outfit) plan, in parallel."""
    settings = get_settings()
    guidelines = req.style_guidelines
    guidelines = settings.style_guidelines if guidelines is None else guidelines
    upscale_face = settings.upscale_face
    (body, reference_face, wearer_face), plan = await asyncio.gather(
        inputs.wearer(req.person_image, upscale_face=upscale_face),
        _plan(store, classification, chosen, guidelines),
    )
    for label, data in (
        ("wearer-body", body),
        ("face-crop", reference_face),
        ("plan", plan),
    ):
        trace.save(label, data)
    return TryOnContext(
        classification=classification,
        wearer_body=body,
        wearer_face=wearer_face,
        reference_face=reference_face,
        style_guidelines=guidelines,
        scenario=req.scenario or settings.scenario,
        params=req.image or ImageParams(),
        chosen=chosen,
        plan=plan,
    )


def _single_refs(product: Product, view: ViewName) -> list[ProductRef]:
    """Primary + secondary image of the single-product route for ``view``:
    the front's primary is the hero (its first image), then its best other
    front upload; the back's, its back uploads (reference crops go along
    separately, ``outfit.extra_crops``)."""
    refs = outfit.uploads_only(product).refs_for(view)
    if view == "front":
        hero = product.refs[0]
        refs = [hero, *(r for r in refs if r is not hero)]
    return refs[:2]


def _refs_by_product(
    ctx: TryOnContext, view: ViewName
) -> list[tuple[Product, list[ProductRef]]]:
    """Each product with its images for ``view``, the chosen one first."""
    c = ctx.classification
    if c.mode == "single":
        return [(c.products[0], _single_refs(c.products[0], view))]
    return [(p, [ref]) for p, ref in ctx.chosen]


def _spec(
    ctx: TryOnContext,
    view: ViewName,
    front_result: bytes | None = None,
    details: list[tuple[str, Detail]] | None = None,
) -> PromptSpec:
    c = ctx.classification
    refs_by_product = _refs_by_product(ctx, view)
    products = [
        PromptProduct(p.id, p.category, refs, p.description)
        for p, refs in refs_by_product
    ]
    shown = [r.image for _, refs in refs_by_product for r in refs]
    single = c.products[0] if c.mode == "single" else None
    return PromptSpec(
        framing=c.framing,
        span=single.span if single else None,
        view=view,
        wearer_body=ctx.wearer_body,
        wearer_face=ctx.wearer_face,
        products=products,
        scenario=ctx.scenario,
        front_result=front_result,
        composition_plan=ctx.plan,
        style_guidelines=ctx.style_guidelines or None,
        crops=[] if front_result else outfit.extra_crops(c.products, shown),
        mannequin_crops=outfit.mannequin_crops(c.products),
        closeups=details or [],
        aspect_ratio=ctx.params.aspect_ratio,
    )


async def _variations(
    ctx: TryOnContext,
    policy: RunPolicy,
    deadline: Deadline,
    view: ViewName,
    front_result: bytes | None = None,
    details: list[tuple[str, Detail]] | None = None,
) -> AsyncGenerator[Event, None]:
    """The runner's events for ``view``; the prompt is built first, in a
    thread (an outfit's product cards are drawn there). With
    ``GENMEDIA_TRYON_FIX`` a rejected image gets one fix pass (``fix``)."""
    spec = _spec(ctx, view, front_result, details)
    system, parts = await _within(
        deadline, asyncio.to_thread(build_prompt, spec)
    )

    async def make(_index: int) -> bytes:
        return await _within(
            deadline,
            asyncio.to_thread(
                gen.generate_image, parts, system=system, params=ctx.params
            ),
        )

    evaluate = evaluator(ctx, view, spec.products)

    async def evaluate_within(image: bytes) -> tuple[Verdict, bytes]:
        return await _within(deadline, evaluate(image))

    judge: Evaluate[bytes] = evaluate_within
    if get_settings().tryon_fix:
        within = functools.partial(_within, deadline)
        judge = fix.fixing(spec, parts, ctx.params, within, evaluate_within)
    events = run_variations(policy, make, judge)
    async with contextlib.aclosing(trace.scoped(events, view=view)) as scoped:
        async for event in scoped:
            yield event


async def back_view(
    prepared: Prepared, front: bytes
) -> AsyncGenerator[VariationEvent, None]:
    """Back variations drawn from ``front``; the ranking rides on the last."""
    pending: VariationEvent | None = None
    ctx, policy, deadline = prepared.ctx, prepared.policy, prepared.deadline
    if ctx is None or policy is None or deadline is None:
        raise RuntimeError("back_view before run_tryon prepared the run")
    variations = _variations(ctx, policy, deadline, "back", front)
    async with contextlib.aclosing(variations):
        async for event in variations:
            if isinstance(event, VariationEvent):
                event.extra = {"view": "back"}
                if pending is not None:
                    yield pending
                pending = event
            elif isinstance(event, CompleteEvent) and pending is not None:
                pending.extra.update(
                    ranked=event.ranked_indices, best_index=event.best_index
                )
    if pending is not None:
        yield pending


def run_tryon(
    req: TryOnRequest, prepared: Prepared | None = None
) -> AsyncGenerator[Event, None]:
    """Run one image try-on and stream its events (spec §3.16).

    Its debug trace (``core.trace``) is stage "image", also in video try-on.
    """
    return trace.scoped(_run_tryon(req, prepared or Prepared()), stage="image")


async def _classify(req: TryOnRequest, deadline: Deadline):
    """One classifier call, the worn photos on a mannequin (``worn``);
    then, at once, the box calls and the close-up search
    (``closeups.search``) of each product's photos. Returns the request
    with the converted photos, the classification, each product's hero,
    the crops by name and the search."""
    found = await _within(
        deadline,
        asyncio.to_thread(
            classify_products, req.product_images, req.reference_model_image
        ),
    )
    req = await _within(deadline, worn.normalise(req, found))
    model = req.reference_model_image
    photos = [*req.product_images, *([] if model is None else [model])]
    search = closeups.search(found, photos[: len(found.photos)])
    try:
        classification, crops = await _within(
            deadline,
            reference.complete(
                found,
                req.product_images,
                req.reference_model_image,
                req.complete_look,
                upscale_crops=req.upscale_crops,
            ),
        )
    except BaseException:
        if search is not None:
            search.cancel()
        raise
    chosen = outfit.choose_garment_images(classification)
    return req, classification, chosen, crops, search


async def _analysis(
    req: TryOnRequest, hit: Analysis | None, deadline: Deadline
):
    """``_classify``'s result, or the kept analysis (``hit``) with this
    request's worn photos on a mannequin (``worn``) in it."""
    if hit is None:
        return await _classify(req, deadline)
    req = await _within(deadline, worn.normalise(req, hit.classification))
    return (req, *hit.parts())


async def _run_tryon(
    req: TryOnRequest, prepared: Prepared
) -> AsyncGenerator[Event, None]:
    search = None
    try:
        _validate(req)
        policy = req.policy or RunPolicy.from_settings()
        deadline = Deadline(policy.deadline_s)
        store, marker = TryOnCache(req), degraded.Marker()
        trimming = asyncio.to_thread(inputs.trimmed, req)
        req, hit = await _within(deadline, store.analysis(trimming))
        with degraded.tracking(marker):  # the analysis's fallbacks
            req, classification, chosen, crops, search = await _analysis(
                req, hit, deadline
            )
        payload = record.payload(
            classification, chosen, req.product_images, crops
        )
        trace.save("classification", payload)
        yield ClassifiedEvent(payload=payload, classification=classification)
        ctx = await _within(
            deadline, _prepare(req, classification, chosen, store)
        )
        prepared.ctx, prepared.policy, prepared.deadline = ctx, policy, deadline
        yield ProgressEvent("preprocessed", "Generating the front view")

        fronts: dict[int | None, bytes | None] = {}  # best_index can be None
        complete = CompleteEvent(ranked_indices=[], best_index=None)
        with degraded.tracking(marker):
            pending = closeups.collect(search, classification.products)
            found = await _within(deadline, pending)  # the drawing waits
        store.save(req, classification, crops, search, marker)
        for kept in record.events(
            payload, chosen, req.product_images, crops, found
        ):
            yield kept
        variations = _variations(
            ctx, policy, deadline, "front", details=found or []
        )
        async with contextlib.aclosing(variations):
            async for event in variations:
                if isinstance(event, VariationEvent):
                    event.extra = {"view": "front"}
                    fronts[event.index] = event.media
                    yield event
                elif isinstance(event, CompleteEvent):
                    complete = event
        best_front = fronts.get(complete.best_index)
        if req.generate_back and prepared.has_back and best_front is not None:
            yield ProgressEvent("back", "Generating the back view")
            back = back_view(prepared, best_front)
            async with contextlib.aclosing(back):
                async for event in back:
                    yield event
        yield complete
    except Exception as exc:
        logger.warning(
            f"try-on failed: {exc}", exc_info=not isinstance(exc, InputError)
        )
        yield ErrorEvent(
            message=user_message(exc),
            retryable=getattr(exc, "retryable", False),
        )
    finally:
        if search is not None:
            search.cancel()  # no-op once done; else it is not needed
