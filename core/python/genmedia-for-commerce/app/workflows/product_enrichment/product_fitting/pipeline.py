# Copyright 2025 Google LLC
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

"""Core fitting pipeline: classification, description, sequential retry generation.

Shared by both the SSE (testing) and JSON (production) endpoints.
"""

import asyncio
import base64
import json
import logging
import os

from fastapi.concurrency import run_in_threadpool
from workflows.shared.debug_utils import (
    get_current_session_key,
    save_debug_image,
)
from workflows.shared.image_utils import preprocess_images
from workflows.shared.nano_banana import NANO_TIMEOUT_SECONDS

from .classification import (
    classify_garments,
    describe_garment_detailed,
    get_framing,
    select_best_back,
    select_best_front,
)
from .garment_eval import (
    evaluate_footwear,
    evaluate_garments,
    evaluate_wearing_quality,
)
from .generation import (
    fix_fitting,
    generate_fitting,
    generate_fitting_back_from_front,
)

logger = logging.getLogger(__name__)


FRAMING_TO_MODEL_PHOTO = {
    "full_body": "top",
    "upper_body": "top",
    "head": "top",
    "lower_body": "bottom",
    "footwear": "bottom",
}


def _model_photo_key(framing: str) -> str:
    """Map framing value to the model photo key prefix (e.g. 'front_top')."""
    suffix = FRAMING_TO_MODEL_PHOTO.get(framing, framing)
    return f"front_{suffix}"


async def _evaluate_image(
    genai_client,
    view_name: str,
    image: bytes,
    garment_imgs: list[bytes],
    eval_view_details: str,
    desc_general: str,
    product_id: str | None = None,
    category: str = "",
):
    """Evaluate a single image (garment accuracy + wearing quality in parallel)."""
    if category == "footwear":
        # Footwear eval already includes image quality/artifact checks
        footwear_desc = f"{desc_general}\n{eval_view_details}".strip()
        garments_eval = await run_in_threadpool(
            evaluate_footwear,
            genai_client,
            image,
            garment_imgs,
            os.getenv("MODEL_NAME_GENERATED_4"),
            footwear_desc,
        )
        wearing_eval = {"score": 3, "explanation": "Included in footwear eval"}
    else:
        garments_eval, wearing_eval = await asyncio.gather(
            run_in_threadpool(
                evaluate_garments,
                genai_client,
                image,
                garment_imgs,
                os.getenv("MODEL_NAME_GENERATED_4"),
                eval_view_details,
                desc_general,
            ),
            run_in_threadpool(
                evaluate_wearing_quality,
                genai_client,
                image,
                os.getenv("MODEL_NAME_GENERATED_4"),
            ),
        )
    details = garments_eval.get("garment_details", [])
    score = garments_eval["garments_score"]
    is_discarded = garments_eval["discard"] or wearing_eval["score"] <= 1
    return garments_eval, wearing_eval, details, score, is_discarded


async def _try_fix(
    view_name: str,
    image: bytes,
    garment_imgs: list[bytes],
    garments_eval: dict,
    *,
    nano_client,
    genai_client,
    gen_message: list,
    gen_config,
    generation_model: str,
    nano_timeout: int,
    eval_view_details: str,
    desc_general: str,
    product_id: str | None = None,
    category: str = "",
    wearing_eval: dict | None = None,
):
    """Try to fix an image and evaluate the fix. Returns (fixed_result_or_None, fix_log)."""
    request_logger = logging.LoggerAdapter(
        logger, {"product_id": product_id or "unknown"}
    )
    details = garments_eval.get("garment_details", [])
    feedback_parts = [
        f"Garment {i}: score={d['score']}/10 -- {d['explanation']}"
        for i, d in enumerate(details)
    ]
    if wearing_eval and wearing_eval.get("score", 3) <= 1:
        feedback_parts.append(
            f"Wearing quality: score={wearing_eval['score']}/3 -- {wearing_eval.get('explanation', 'Poor wearing quality')}"
        )
    eval_feedback = "\n".join(feedback_parts)

    try:
        fixed_image = await run_in_threadpool(
            fix_fitting,
            nano_client,
            gen_message,
            gen_config,
            image,
            eval_feedback,
            generation_model,
            nano_timeout,
        )
        if fixed_image is None:
            request_logger.debug(
                f"[Fitting] {view_name} fix -> FAILED (no image)"
            )
            return None, {"status": "failed"}

        (
            fixed_eval,
            fixed_wearing,
            fixed_details,
            fixed_score,
            fixed_discarded,
        ) = await _evaluate_image(
            genai_client,
            view_name,
            fixed_image,
            garment_imgs,
            eval_view_details,
            desc_general,
            product_id=product_id,
            category=category,
        )
        for di, d in enumerate(fixed_details):
            request_logger.debug(
                f"[Fitting] {view_name} fix garment {di}: score={d['score']}/10 -- {d['explanation']}"
            )
        request_logger.info(
            f"[Fitting] {view_name} fix score={fixed_score:.0f}/100, discard={fixed_discarded}, wearing={fixed_wearing['score']}/3 -- {fixed_wearing.get('explanation', '')}"
        )

        fix_log = {
            "garments_score": fixed_score,
            "garment_details": [
                {"score": d["score"], "explanation": d["explanation"]}
                for d in fixed_details
            ],
            "discard": fixed_eval["discard"],
            "wearing_score": fixed_wearing["score"],
            "wearing_explanation": fixed_wearing["explanation"],
        }

        fix_result = {
            "image": fixed_image,
            "garments_evaluation": fixed_eval,
            "wearing_evaluation": fixed_wearing,
            "score": fixed_score,
            "is_discarded": fixed_discarded,
        }
        return fix_result, fix_log

    except Exception as e:
        request_logger.error(f"[Fitting] {view_name} fix error: {e}")
        return None, {"status": "error", "error": str(e)}


async def _generate_view(
    view_name: str,
    garment_imgs: list[bytes],
    model_img: bytes | None,
    max_retries: int,
    *,
    nano_client,
    genai_client,
    scenario: str,
    framing: str,
    generation_model: str,
    garment_description: dict,
    gender: str | None,
    front_view_details: str,
    back_view_details: str,
    nano_timeout: int = NANO_TIMEOUT_SECONDS,
    gen_func=None,
    gen_kwargs: dict | None = None,
    product_id: str | None = None,
    category: str = "",
    session_key: str | None = None,
):
    """Generate images sequentially with fix attempts, up to max_retries.

    Flow per attempt:
      1. Generate 1 image -> evaluate
      2. If score == 100 -> done
      3. Try fix -> evaluate -> keep best (original vs fix)
      4. If best == 100 -> done

    After all attempts:
      5. If best across all attempts < 90, try one final fix on the overall best

    Returns (best_result_dict_or_None, list_of_sse_event_strings, attempts_log).
    """
    request_logger = logging.LoggerAdapter(
        logger, {"product_id": product_id or "unknown"}
    )
    sse_events: list[str] = []
    attempts_log: list[dict] = []
    eval_view_details = (
        back_view_details if view_name == "back" else front_view_details
    )
    desc_general = (
        garment_description.get("general", "") if garment_description else ""
    )

    best: dict | None = None  # single best result (discarded or not)

    def _is_better(score, is_discarded):
        """Check if a new result is better than current best. Non-discarded always beats discarded."""
        if best is None:
            return True
        best_is_discarded = best.get("final_score") is None
        best_score = (
            best.get("final_score")
            or best["garments_evaluation"]["garments_score"]
        )
        # Non-discarded always beats discarded
        if best_is_discarded and not is_discarded:
            return True
        if not best_is_discarded and is_discarded:
            return False
        return score > best_score

    def _update_best(
        image,
        garments_eval,
        wearing_eval,
        score,
        is_discarded,
        attempt_idx,
        gen_msg=None,
        gen_cfg=None,
    ):
        nonlocal best
        if not _is_better(score, is_discarded):
            return
        best = {
            "index": attempt_idx,
            "image": image,
            "garments_evaluation": garments_eval,
            "wearing_evaluation": wearing_eval,
            "gen_message": gen_msg,
            "gen_config": gen_cfg,
        }
        if not is_discarded:
            best["final_score"] = score

    def _best_score():
        if best is None:
            return 0
        return (
            best.get("final_score")
            or best["garments_evaluation"]["garments_score"]
        )

    def _emit_sse(
        index,
        image,
        garments_eval,
        is_discarded,
        score,
        fix=False,
        final_fix=False,
    ):
        event = {
            "index": index,
            "view": view_name,
            "status": "discarded" if is_discarded else "ready",
            "image_base64": base64.b64encode(image).decode("utf-8"),
            "garments_evaluation": garments_eval,
        }
        if not is_discarded:
            event["final_score"] = score
        if fix:
            event["fix"] = True
        if final_fix:
            event["final_fix"] = True
        sse_events.append(f"data: {json.dumps(event)}\n\n")

    def _is_good_enough(score, is_discarded):
        return score >= 90 and not is_discarded

    fix_kwargs = {
        "nano_client": nano_client,
        "genai_client": genai_client,
        "generation_model": generation_model,
        "nano_timeout": nano_timeout,
        "eval_view_details": eval_view_details,
        "desc_general": desc_general,
        "product_id": product_id,
        "category": category,
    }

    for attempt in range(max_retries):
        request_logger.info(
            f"[Fitting] {view_name} attempt {attempt + 1}/{max_retries}"
        )

        # -- Generate --
        gen_message = None
        gen_config = None
        try:
            if gen_func is not None:
                (
                    fitting_image,
                    gen_message,
                    gen_config,
                ) = await run_in_threadpool(gen_func, **gen_kwargs)
            else:
                (
                    fitting_image,
                    gen_message,
                    gen_config,
                ) = await run_in_threadpool(
                    generate_fitting,
                    nano_client,
                    scenario,
                    garment_imgs,
                    model_img,
                    framing,
                    generation_model,
                    view_name,
                    garment_description,
                    gender,
                    nano_timeout,
                )
        except Exception as e:
            request_logger.error(
                f"[Fitting] {view_name} attempt {attempt + 1} generation error: {e}"
            )
            fitting_image = None

        if fitting_image:
            save_debug_image(
                fitting_image,
                f"attempt_{attempt + 1}",
                prefix=f"{view_name}_{session_key}",
            )

        if fitting_image is None:
            request_logger.info(
                f"[Fitting] {view_name} attempt {attempt + 1} -> FAILED (no image)"
            )
            sse_events.append(
                f"data: {json.dumps({'index': attempt, 'view': view_name, 'status': 'failed', 'error': 'Generation failed'})}\n\n"
            )
            attempts_log.append(
                {
                    "attempt": attempt,
                    "status": "failed",
                    "generation": None,
                    "fix": None,
                    "winner": None,
                }
            )
            continue

        # -- Evaluate --
        (
            garments_eval,
            wearing_eval,
            details,
            score,
            is_discarded,
        ) = await _evaluate_image(
            genai_client,
            view_name,
            fitting_image,
            garment_imgs,
            eval_view_details,
            desc_general,
            product_id=product_id,
            category=category,
        )
        for di, d in enumerate(details):
            request_logger.debug(
                f"[Fitting] {view_name} attempt {attempt + 1} garment {di}: score={d['score']}/10 -- {d['explanation']}"
            )
        request_logger.info(
            f"[Fitting] {view_name} attempt {attempt + 1}/{max_retries} score={score:.0f}/100, discard={is_discarded}, wearing={wearing_eval['score']}/3 -- {wearing_eval.get('explanation', '')}"
        )

        gen_eval_summary = {
            "garments_score": score,
            "garment_details": [
                {"score": d["score"], "explanation": d["explanation"]}
                for d in details
            ],
            "discard": garments_eval["discard"],
            "wearing_score": wearing_eval["score"],
            "wearing_explanation": wearing_eval["explanation"],
        }

        _emit_sse(attempt, fitting_image, garments_eval, is_discarded, score)
        _update_best(
            fitting_image,
            garments_eval,
            wearing_eval,
            score,
            is_discarded,
            attempt,
            gen_message,
            gen_config,
        )

        if _is_good_enough(score, is_discarded):
            attempts_log.append(
                {
                    "attempt": attempt,
                    "status": "accepted",
                    "generation": gen_eval_summary,
                    "fix": None,
                    "winner": "generation",
                }
            )
            request_logger.info(
                f"[Fitting] {view_name} attempt {attempt + 1} -> good score, done"
            )
            break

        # -- Fix this attempt --
        request_logger.info(
            f"[Fitting] {view_name} attempt {attempt + 1}/{max_retries} trying fix"
        )
        fix_result, fix_log = await _try_fix(
            view_name,
            fitting_image,
            garment_imgs,
            garments_eval,
            gen_message=gen_message,
            gen_config=gen_config,
            wearing_eval=wearing_eval,
            **fix_kwargs,
        )

        winner = "generation"
        if fix_result is not None:
            fixed_score = fix_result["score"]
            fixed_discarded = fix_result["is_discarded"]
            _emit_sse(
                attempt,
                fix_result["image"],
                fix_result["garments_evaluation"],
                fixed_discarded,
                fixed_score,
                fix=True,
            )

            if _is_better(fixed_score, fixed_discarded):
                request_logger.info(
                    f"[Fitting] {view_name} attempt {attempt + 1}/{max_retries} fix improved: {score:.0f} -> {fixed_score:.0f} (discard: {is_discarded} -> {fixed_discarded})"
                )
                _update_best(
                    fix_result["image"],
                    fix_result["garments_evaluation"],
                    fix_result["wearing_evaluation"],
                    fixed_score,
                    fixed_discarded,
                    attempt,
                    gen_message,
                    gen_config,
                )
                winner = "fix"
            else:
                request_logger.info(
                    f"[Fitting] {view_name} attempt {attempt + 1}/{max_retries} fix no improvement: {score:.0f} -> {fixed_score:.0f} (discard: {is_discarded} -> {fixed_discarded})"
                )

            if _is_good_enough(fixed_score, fixed_discarded):
                attempts_log.append(
                    {
                        "attempt": attempt,
                        "status": "accepted",
                        "generation": gen_eval_summary,
                        "fix": fix_log,
                        "winner": winner,
                    }
                )
                request_logger.info(
                    f"[Fitting] {view_name} attempt {attempt + 1} -> fix good score, done"
                )
                break

        attempts_log.append(
            {
                "attempt": attempt,
                "status": "discarded" if is_discarded else "accepted",
                "generation": gen_eval_summary,
                "fix": fix_log,
                "winner": winner,
            }
        )

    # -- Final fix: if best across all attempts < 90, try one last fix on the overall best --
    if best is not None and _best_score() < 90:
        request_logger.info(
            f"[Fitting] {view_name}: all {max_retries} attempts done, best score={_best_score():.0f}, trying final fix"
        )
        fix_result, fix_log = await _try_fix(
            view_name,
            best["image"],
            garment_imgs,
            best["garments_evaluation"],
            gen_message=best["gen_message"],
            gen_config=best["gen_config"],
            wearing_eval=best.get("wearing_evaluation"),
            **fix_kwargs,
        )
        if fix_result is not None and _is_better(
            fix_result["score"], fix_result["is_discarded"]
        ):
            request_logger.info(
                f"[Fitting] {view_name} final fix improved: {_best_score():.0f} -> {fix_result['score']:.0f}"
            )
            _update_best(
                fix_result["image"],
                fix_result["garments_evaluation"],
                fix_result["wearing_evaluation"],
                fix_result["score"],
                fix_result["is_discarded"],
                best["index"],
                best["gen_message"],
                best["gen_config"],
            )
            _emit_sse(
                best["index"],
                fix_result["image"],
                fix_result["garments_evaluation"],
                fix_result["is_discarded"],
                fix_result["score"],
                fix=True,
                final_fix=True,
            )
            attempts_log.append(
                {
                    "attempt": "final_fix",
                    "status": "improved",
                    "fix": fix_log,
                    "winner": "fix",
                }
            )
        else:
            request_logger.info(
                f"[Fitting] {view_name} final fix did not improve"
            )
            attempts_log.append(
                {
                    "attempt": "final_fix",
                    "status": "no_improvement",
                    "fix": fix_log,
                    "winner": "generation",
                }
            )

    # Log final outcome
    if best is None:
        request_logger.info(
            f"[Fitting] {view_name}: all attempts failed, no image produced"
        )
    elif best.get("final_score") is None:
        disc_score = best["garments_evaluation"]["garments_score"]
        request_logger.info(
            f"[Fitting] {view_name}: best was discarded (score={disc_score:.0f}), using as fallback"
        )
    else:
        request_logger.info(
            f"[Fitting] {view_name}: final score={best['final_score']:.0f}"
        )

    return best, sse_events, attempts_log


def _build_validation(result: dict | None) -> dict | None:
    """Build a compact validation dict from a pipeline result."""
    if result is None:
        return None
    garments_eval = result.get("garments_evaluation", {})
    wearing_eval = result.get("wearing_evaluation", {})
    garment_discard = garments_eval.get("discard", False)
    wearing_discard = wearing_eval.get("score", 3) <= 1
    return {
        "garments_score": garments_eval.get("garments_score"),
        "garment_details": garments_eval.get("garment_details", []),
        "discard": garment_discard or wearing_discard,
        "wearing_score": wearing_eval.get("score"),
        "wearing_explanation": wearing_eval.get("explanation", ""),
    }


async def run_fitting_pipeline(
    garment_images_bytes: list[bytes],
    model_photo_map: dict[str, bytes],
    max_retries: int,
    scenario: str,
    generation_model: str,
    gender: str | None,
    nano_client,
    genai_client,
    nano_timeout: int = NANO_TIMEOUT_SECONDS,
    product_id: str | None = None,
):
    """Run the full fitting pipeline: classify -> describe -> generate with retries.

    Returns a dict with:
      - front: {image, validation, status} or None
      - back:  {image, validation, status} or None
      - classification: garment classification (category, description, views)
      - framing: str
      - sse_events: list of SSE event strings (for the streaming endpoint)
    """
    request_logger = logging.LoggerAdapter(
        logger, {"product_id": product_id or "unknown"}
    )
    all_sse_events: list[str] = []

    session_key = get_current_session_key()
    request_logger.info(
        f"[Fitting][Key:{session_key}] Preprocessing garment images (extract + upscale)"
    )
    garment_images_bytes = await run_in_threadpool(
        preprocess_images,
        garment_images_bytes,
        client=genai_client,
        upscale_client=genai_client,
        upscale_images=True,
        create_canva=False,
        skip_crop=True,
    )

    # Step 1: Classify garments (single call for all images of the same garment)
    request_logger.info(
        f"[Fitting][Key:{session_key}] Step 1: Classifying garments"
    )
    classification = await run_in_threadpool(
        classify_garments,
        genai_client,
        garment_images_bytes,
    )

    category = classification["category"]
    description = classification.get("description", "")
    views = classification.get("views", [])
    request_logger.info(
        f"[Fitting] Classification: category={category}, description='{description}'"
    )

    for v in views:
        request_logger.debug(f"[Fitting] Image {v['index']}: view={v['view']}")

    # Step 2: Determine framing
    framing = get_framing(category)
    request_logger.debug(f"[Fitting] Framing: {framing}")

    # Step 3: For footwear/head accessories, skip view classification — all images are candidates, select best 2
    if category in ("footwear", "head_accessory"):
        request_logger.info(
            f"[Fitting] {category} detected: selecting best 2 from all {len(garment_images_bytes)} images"
        )
        front_selection = await run_in_threadpool(
            select_best_front,
            genai_client,
            garment_images_bytes,
            category=category,
        )
        front_indices = [
            i
            for i in front_selection.get("best_indices", [])
            if i < len(garment_images_bytes)
        ]
        back_indices = []
        front_garments = [garment_images_bytes[i] for i in front_indices]
        back_garments = []
        back_selection = {
            "best_indices": [],
            "evaluations": [],
            "rejected_views": [],
        }
    else:
        # Split images by view, then select best front/back in parallel
        front_candidate_indices = [
            v["index"] for v in views if v["view"] == "front"
        ]
        back_candidate_indices = [
            v["index"] for v in views if v["view"] == "back"
        ]
        front_candidate_images = [
            garment_images_bytes[i] for i in front_candidate_indices
        ]
        back_candidate_images = [
            garment_images_bytes[i] for i in back_candidate_indices
        ]
        request_logger.debug(
            f"[Fitting] Front candidates: {front_candidate_indices}, Back candidates: {back_candidate_indices}"
        )

        # Run selection in parallel, only on the relevant subsets
        selection_tasks = []
        has_front_candidates = bool(front_candidate_images)
        has_back_candidates = bool(back_candidate_images)

        if has_front_candidates:
            selection_tasks.append(
                run_in_threadpool(
                    select_best_front,
                    genai_client,
                    front_candidate_images,
                    category=category,
                )
            )
        if has_back_candidates:
            selection_tasks.append(
                run_in_threadpool(
                    select_best_back,
                    genai_client,
                    back_candidate_images,
                    category=category,
                )
            )

        selection_results = (
            await asyncio.gather(*selection_tasks) if selection_tasks else []
        )

        # Unpack results and remap indices back to original image list
        result_idx = 0
        if has_front_candidates:
            front_selection = selection_results[result_idx]
            result_idx += 1
            front_indices = [
                front_candidate_indices[i]
                for i in front_selection.get("best_indices", [])
                if i < len(front_candidate_indices)
            ]
        else:
            front_selection = {
                "best_indices": [],
                "evaluations": [],
                "rejected_views": [],
            }
            front_indices = []

        if has_back_candidates:
            back_selection = selection_results[result_idx]
            back_indices = [
                back_candidate_indices[i]
                for i in back_selection.get("best_indices", [])
                if i < len(back_candidate_indices)
            ]
        else:
            back_selection = {
                "best_indices": [],
                "evaluations": [],
                "rejected_views": [],
            }
            back_indices = []

        front_garments = [garment_images_bytes[i] for i in front_indices]
        back_garments = [garment_images_bytes[i] for i in back_indices]

    request_logger.debug(
        f"[Fitting] Selected front indices: {front_indices}, back indices: {back_indices}"
    )
    request_logger.debug(
        f"[Fitting] Front garments: {len(front_garments)}, Back garments: {len(back_garments)}"
    )

    # Build skip reasons from rejected views (valid garments with imperfect angles)
    front_rejected = front_selection.get("rejected_views", [])
    back_rejected = back_selection.get("rejected_views", [])

    front_skipped_reason = None
    back_skipped_reason = None

    if not front_garments and front_rejected:
        rejected_desc = ", ".join(
            f"{rv.get('view_angle', 'unknown')} (image {rv.get('index')})"
            for rv in front_rejected
        )
        front_skipped_reason = (
            f"No usable front view found. Available images were: {rejected_desc}. "
            "Please provide a front-facing product photo."
        )
        request_logger.debug(f"[Fitting] Front skipped: {front_skipped_reason}")

    if not back_garments and back_rejected:
        rejected_desc = ", ".join(
            f"{rv.get('view_angle', 'unknown')} (image {rv.get('index')})"
            for rv in back_rejected
        )
        back_skipped_reason = (
            f"No usable back view found. Available images were: {rejected_desc}. "
            "Please provide a back-facing product photo."
        )
        request_logger.debug(f"[Fitting] Back skipped: {back_skipped_reason}")

    if not front_garments and not back_garments:
        error_msg = "No valid garment images found."
        if front_rejected or back_rejected:
            parts = []
            if front_rejected:
                parts.append(f"Front: {front_skipped_reason}")
            if back_rejected:
                parts.append(f"Back: {back_skipped_reason}")
            error_msg = "No usable front or back view found. " + " ".join(parts)
        else:
            error_msg = "No valid garment images found. Images must show a single garment not worn by a person."
        return {
            "front": None,
            "back": None,
            "error": error_msg,
            "classification": classification,
            "framing": framing,
            "sse_events": [],
        }

    # Prepare front view info
    front_model_photo = None
    if front_garments:
        model_photo_key = _model_photo_key(framing)
        front_model_photo = model_photo_map[model_photo_key]
        request_logger.debug(
            f"[Fitting] Will generate front view ({len(front_garments)} ref images, indices {front_indices}) using model photo: {model_photo_key}"
        )
    if back_garments:
        request_logger.debug(
            f"[Fitting] Back garments available ({len(back_garments)} ref images, indices {back_indices}) -- will generate after front"
        )

    has_front = bool(front_garments)
    has_back = bool(back_garments)

    # Selection SSE event
    selection_info = {
        "status": "selection",
        "front_indices": front_indices,
        "back_indices": back_indices,
        "front_eval_index": front_indices[0] if front_indices else None,
        "back_eval_index": back_indices[0] if back_indices else None,
        "framing": framing,
        "category": category,
        "description": description,
        "views": views,
    }
    if front_skipped_reason:
        selection_info["front_skipped_reason"] = front_skipped_reason
    if back_skipped_reason:
        selection_info["back_skipped_reason"] = back_skipped_reason
    all_sse_events.append(f"data: {json.dumps(selection_info)}\n\n")

    # Step 4: Generate detailed garment description
    garment_description = await run_in_threadpool(
        describe_garment_detailed,
        genai_client,
        front_garments,
        back_garments,
    )
    front_view_details = garment_description.get("front_details", "")
    back_view_details = garment_description.get("back_details", "")
    request_logger.debug(
        f"[Fitting] Garment description: general='{garment_description.get('general', '')}'"
    )
    request_logger.debug(
        f"[Fitting] Garment description: front_details='{front_view_details}'"
    )
    request_logger.debug(
        f"[Fitting] Garment description: back_details='{back_view_details}'"
    )

    # Shared kwargs for _generate_view
    shared_kwargs = {
        "nano_client": nano_client,
        "genai_client": genai_client,
        "scenario": scenario,
        "framing": framing,
        "generation_model": generation_model,
        "session_key": session_key,
        "garment_description": garment_description,
        "gender": gender,
        "front_view_details": front_view_details,
        "back_view_details": back_view_details,
        "nano_timeout": nano_timeout,
        "product_id": product_id,
        "category": category,
    }

    front_result = None
    back_result = None
    front_attempts_log: list[dict] = []
    back_attempts_log: list[dict] = []

    # -- Back-only fallback (no front garments) --
    if not has_front and has_back:
        model_photo_key = _model_photo_key(framing)
        front_model_as_ref = model_photo_map[model_photo_key]
        request_logger.debug(
            f"[Fitting] No front garments -- generating back from front model photo ({model_photo_key})"
        )

        gen_kwargs = {
            "client": nano_client,
            "scenario": scenario,
            "back_garment_images": back_garments,
            "best_front_image": front_model_as_ref,
            "framing": framing,
            "model": generation_model,
            "garment_description": garment_description,
            "model_gender": gender,
            "timeout": nano_timeout,
        }
        back_result, back_events, back_attempts_log = await _generate_view(
            "back",
            back_garments,
            None,
            max_retries,
            gen_func=generate_fitting_back_from_front,
            gen_kwargs=gen_kwargs,
            **shared_kwargs,
        )
        all_sse_events.extend(back_events)

    else:
        # -- PHASE 1: Front generation --
        if has_front:
            request_logger.info(
                f"[Fitting] Phase 1: Generating front (up to {max_retries} attempts)"
            )
            (
                front_result,
                front_events,
                front_attempts_log,
            ) = await _generate_view(
                "front",
                front_garments,
                front_model_photo,
                max_retries,
                **shared_kwargs,
            )
            all_sse_events.extend(front_events)

        # -- PHASE 2: Back generation (from best front) --
        best_front_image = front_result["image"] if front_result else None
        if best_front_image:
            status = (
                "ready"
                if front_result.get("final_score") is not None
                else "discarded"
            )
            request_logger.debug(
                f"[Fitting] Best front image: attempt {front_result['index']} (status={status})"
            )
        else:
            request_logger.debug(
                "[Fitting] No front image produced (all failed)"
            )

        if has_back and best_front_image is not None:
            request_logger.info(
                f"[Fitting] Phase 2: Generating back (up to {max_retries} attempts) from best front"
            )
            gen_kwargs = {
                "client": nano_client,
                "scenario": scenario,
                "back_garment_images": back_garments,
                "best_front_image": best_front_image,
                "framing": framing,
                "model": generation_model,
                "garment_description": garment_description,
                "model_gender": gender,
                "timeout": nano_timeout,
            }
            back_result, back_events, back_attempts_log = await _generate_view(
                "back",
                back_garments,
                None,
                max_retries,
                gen_func=generate_fitting_back_from_front,
                gen_kwargs=gen_kwargs,
                **shared_kwargs,
            )
            all_sse_events.extend(back_events)
        elif has_back:
            request_logger.debug(
                "[Fitting] Skipping back generation -- no valid front image available"
            )

    # Build response
    def _format_side(result: dict | None, attempts: list[dict]) -> dict | None:
        if result is None:
            return None
        is_discarded = result.get("final_score") is None
        return {
            "image": result["image"],
            "status": "discarded" if is_discarded else "ready",
            "validation": _build_validation(result),
            "attempts": attempts,
            "total_attempts": len(attempts),
        }

    formatted_front = _format_side(front_result, front_attempts_log)
    formatted_back = _format_side(back_result, back_attempts_log)

    # Add generation failure reasons for views that were attempted but produced no usable image
    if has_front and formatted_front is None and not front_skipped_reason:
        front_skipped_reason = f"Front generation failed after {max_retries} attempts. All images were discarded due to poor wearing quality."
        request_logger.debug(f"[Fitting] {front_skipped_reason}")
    if has_back and formatted_back is None and not back_skipped_reason:
        back_skipped_reason = f"Back generation failed after {max_retries} attempts. All images were discarded due to poor wearing quality."
        request_logger.debug(f"[Fitting] {back_skipped_reason}")

    response = {
        "front": formatted_front,
        "back": formatted_back,
        "classification": classification,
        "framing": framing,
        "sse_events": all_sse_events,
    }
    if front_skipped_reason:
        response["front_skipped_reason"] = front_skipped_reason
    if back_skipped_reason:
        response["back_skipped_reason"] = back_skipped_reason
    return response
