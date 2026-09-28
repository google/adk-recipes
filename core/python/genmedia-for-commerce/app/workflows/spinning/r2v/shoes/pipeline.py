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

"""Pipeline for generating shoe spinning videos using R2V."""

# Standard library imports
import logging
import multiprocessing
import os

from workflows.shared.gcs_utils import save_and_upload_to_gcs
from workflows.shared.image_utils import (
    create_canvas_with_height_scaling,
    extract_upscale_product,
    stack_and_canvas_images,
)
from workflows.shared.image_utils import (
    replace_background as extract_product_from_background,
)
from workflows.shared.utils import predict_parallel
from workflows.shared.veo_utils import generate_veo_r2v

# Project imports
from workflows.spinning.r2v.shoes.classify_shoes import (
    classify_shoe,
    classify_shoe_closure,
)
from workflows.spinning.r2v.shoes.images_utils import (
    image_closure_selection,
    sample_and_process_frames,
)
from workflows.spinning.r2v.shoes.product_consistency_validation import (
    validate_product_consistency,
)
from workflows.spinning.r2v.shoes.prompt_generation_r2v import (
    generate_veo_prompt_r2v,
)
from workflows.spinning.r2v.shoes.shoe_images_selection import (
    classify_video_gen_status,
    pick_images_by_ordered_best_side,
)
from workflows.spinning.r2v.shoes.split_multiple_shoes import (
    divide_duplicate_image,
)
from workflows.spinning.r2v.shoes.video_validation_r2v import (
    validate_and_fix_product_spin_consistency_r2v,
)

logger = logging.getLogger(__name__)


def generate_single_clip_r2v(
    clip_idx,
    reference_images,
    veo_client,
    max_retries,
    veo_model,
    client=None,
    gemini_model=None,
    veo_prompt=None,
    reference_type="asset",
    shoe_classifier_model=None,
    validate_product_consistency_flag=True,
    reference_images_unstacked=None,
    reference_labels_unstacked=None,
    disable_logging=False,
    product_consistency_model=os.getenv("MODEL_NAME_GENERATED_4"),  # noqa: B008
):
    """Generate a single video clip using reference-to-video (R2V) modality.

    Uses reference images instead of start/end frames.

    Args:
        clip_idx: Index of the clip
        reference_images: List of image bytes to use as reference (also used for description)
        veo_client: Veo client for video generation
        max_retries: Maximum retries for video generation
        veo_model: Veo model to use
        client: Optional Gemini client for prompt generation
        gemini_model: Optional Gemini model name for prompt generation
        veo_prompt: Optional prompt for video generation (if None, will be generated)
        reference_type: Type of reference ("asset", "style", etc.)
        shoe_classifier_model: Model endpoint for shoe classification
        validate_product_consistency_flag: If True, validates product consistency (default: True)
        reference_images_unstacked: Unstacked reference image bytes for consistency validation
        reference_labels_unstacked: Labels for unstacked reference images
        disable_logging: If True, disables all logging output (default: True)
        product_consistency_model: Model to use for product consistency validation (default: "gemini-3.6-flash")

    Returns:
        dict: Metadata about the generated clip

    """
    # Save current logger level and disable if requested
    original_level = logger.level
    if disable_logging:
        logger.setLevel(logging.CRITICAL + 1)  # Disable all logging

    try:
        logger.info(
            f"Clip {clip_idx}: Reference-to-Video with {len(reference_images)} reference images"
        )

        # Generate prompt if not provided
        if not veo_prompt:
            logger.info("  Generating R2V prompt...")
            veo_prompt = generate_veo_prompt_r2v(
                client=client,
                gemini_model=gemini_model,
                all_images_bytes=reference_images,
            )

        # Validate prompt generation
        if veo_prompt is None:
            raise Exception(
                "Prompt generation failed. The Gemini service may be temporarily unavailable."
            )

        logger.info(f"  ✓ Prompt: {veo_prompt[:80]}...")

        # Generate video with retries
        retry_count = 0
        video_bytes = None
        original_video_bytes = None

        while retry_count < max_retries:
            logger.info(f"  Generating video (attempt {retry_count})...")
            video_bytes = generate_veo_r2v(
                veo_client,
                reference_images=reference_images,
                prompt=veo_prompt,
                reference_type=reference_type,
                model=veo_model,
            )
            original_video_bytes = video_bytes
            if video_bytes:
                try:
                    (
                        is_valid,
                        reason,
                        new_video_bytes,
                        frame_classifications,
                        sampled_indices,
                        total_frames,
                        frame_list,
                    ) = validate_and_fix_product_spin_consistency_r2v(
                        video_bytes, client, shoe_classifier_model
                    )
                except Exception as validation_err:
                    # Transient errors (SSL, network) during validation — retry
                    retry_count += 1
                    logger.warning(
                        f"  ✗ Validation crashed (attempt {retry_count}/{max_retries}): {type(validation_err).__name__}: {validation_err}"
                    )
                    if retry_count >= max_retries:
                        raise
                    continue

                if is_valid:
                    logger.info(f" ✓ Video generation valid: {reason}")
                    video_bytes = new_video_bytes

                    # Product consistency check
                    if validate_product_consistency_flag:
                        logger.info("Validating product consistency...")
                        try:
                            consistency_valid, consistency_msg, _ = (
                                validate_product_consistency(
                                    video_bytes=video_bytes,
                                    frame_classifications=frame_classifications,
                                    sampled_indices=sampled_indices,
                                    reference_images_bytes=reference_images_unstacked,
                                    reference_labels=reference_labels_unstacked,
                                    client=veo_client,
                                    model=product_consistency_model,
                                )
                            )
                        except Exception as consistency_err:
                            retry_count += 1
                            logger.warning(
                                f"  ✗ Product consistency check crashed (attempt {retry_count}/{max_retries}): {type(consistency_err).__name__}: {consistency_err}"
                            )
                            if retry_count >= max_retries:
                                raise
                            continue

                        if not consistency_valid:
                            retry_count += 1
                            logger.warning(
                                f" Product consistency validation failed: {consistency_msg} (attempt {retry_count}/{max_retries})"
                            )
                            if retry_count >= max_retries:
                                error_msg = f"Max retries ({max_retries}) reached for clip {clip_idx}. Product consistency validation failed: {consistency_msg}"
                                logger.error(f"  {error_msg}")
                                raise Exception(error_msg)
                            continue  # Retry video generation
                        else:
                            logger.info(
                                f"Product consistency validation passed: {consistency_msg}"
                            )
                            break  # Exit retry loop - all validations passed
                    else:
                        # Product consistency check disabled, spin validation passed
                        break  # Exit retry loop

                else:
                    retry_count += 1
                    logger.info(
                        f"  ✗ Video generation not valid: {reason} (attempt {retry_count}/{max_retries})"
                    )

                    if retry_count >= max_retries:
                        error_msg = f"Max retries ({max_retries}) reached for clip {clip_idx}. Validation failed: {reason}"
                        logger.error(f"  {error_msg}")
                        raise Exception(error_msg)
            else:
                retry_count += 1
                logger.warning(
                    f"  ✗ Video generation failed (attempt {retry_count}/{max_retries})"
                )

                if retry_count >= max_retries:
                    error_msg = f"VEO video generation failed after {max_retries} retries. The VEO service may be temporarily unavailable or the request timed out."
                    logger.error(f"  {error_msg}")
                    raise Exception(error_msg)

        logger.info(f"✓ Clip {clip_idx} complete (retry count: {retry_count})")

        return {
            "index": clip_idx,
            "prompt": veo_prompt,
            "duration_seconds": 8,  # R2V always uses 8 seconds
            "retry_count": retry_count,
            "video_bytes": video_bytes,
            "original_video_bytes": original_video_bytes,
            "reference_images_count": len(reference_images),
            "frame_classifications": frame_classifications,  # Classifications for sampled frames
            "sampled_frame_indices": sampled_indices,  # Indices of sampled frames
            "total_frames": total_frames,  # Total frames in video
            "frame_list": frame_list,  # List of all frame bytes (after validation processing)
        }
    finally:
        # Restore original logger level
        logger.setLevel(original_level)


def preprocess_classify_images(
    images_bytes_list,
    client,
    upscale_client,
    shoe_classifier_model,
    num_workers=32,
    upscale_images=True,
    create_canva=True,
):
    """Preprocess and classify input images.

    Args:
        images_bytes_list: List of image bytes.
        client: Gemini client.
        upscale_client: Client for upscaling.
        shoe_classifier_model: Model for shoe classification.
        num_workers: Number of workers for parallel processing. Default: 32.
        upscale_images: Whether to upscale images. Default: True.
        create_canva: Whether to create a canvas. Default: True.

    Returns:
        Tuple of (processed_images, video_gen_status, has_velcro).

    """
    # STEP 1: Classify images (same logic as /classify-images endpoint)
    logger.info("Step 1: Classifying images...")

    initial_predictions = predict_parallel(
        images_bytes_list,
        lambda img_bytes: classify_shoe(
            img_bytes, client, shoe_classifier_model
        ),
        max_workers=num_workers,
        show_progress_bar=False,
    )

    # Check for classification failures
    if None in initial_predictions:
        none_count = initial_predictions.count(None)
        raise Exception(
            f"Image classification failed for {none_count} out of {len(initial_predictions)} images. "
            "The classification service may be temporarily unavailable. Please try again later."
        )

    logger.info(
        f"✓ Initial classification complete: {len(initial_predictions)} predictions. Predictions {initial_predictions}"
    )

    images_to_split = [
        img_bytes
        for img_bytes, pred in zip(
            images_bytes_list, initial_predictions, strict=True
        )
        if pred.strip().lower() == "multiple"
    ]

    single_images_valid = [
        img_bytes
        for img_bytes, pred in zip(
            images_bytes_list, initial_predictions, strict=True
        )
        if pred.strip().lower() not in ["multiple", "invalid"]
    ]

    single_images_pred = [
        pred.strip().lower()
        for _, pred in zip(images_bytes_list, initial_predictions, strict=True)
        if pred.strip().lower() not in ["multiple", "invalid"]
    ]

    # Stage 2: Split images classified as "multiple" (parallelized)
    splitted_valid = []
    splitted_pred = []
    if len(images_to_split) > 0:
        logger.info(
            f"Step 1.2: Splitting {len(images_to_split)} images with multiple products..."
        )
        split_results = predict_parallel(
            images_to_split,
            lambda img_bytes: divide_duplicate_image(
                img_bytes, client, return_masks=False
            ),
            max_workers=num_workers,
            show_progress_bar=False,
        )
        split_images_bytes_list = [
            image_bytes
            for splitted_result_list in split_results
            for image_bytes in splitted_result_list
        ]
        logger.info(
            f"Step 1.2: Classifying {len(split_images_bytes_list)} split images..."
        )
        splitted_predictions = predict_parallel(
            split_images_bytes_list,
            lambda img_bytes: classify_shoe(
                img_bytes, client, shoe_classifier_model
            ),
            max_workers=num_workers,
            show_progress_bar=False,
        )

        # Check for split image classification failures
        if None in splitted_predictions:
            none_count = splitted_predictions.count(None)
            raise Exception(
                f"Image classification failed for {none_count} out of {len(splitted_predictions)} split images. "
                "The classification service may be temporarily unavailable. Please try again later."
            )

        logger.info(f"✓ Split predictions: {splitted_predictions}")
        splitted_valid = [
            img_bytes
            for img_bytes, pred in zip(
                split_images_bytes_list, splitted_predictions, strict=True
            )
            if pred.strip().lower() not in ["multiple", "invalid"]
        ]
        splitted_pred = [
            pred.strip().lower()
            for _, pred in zip(
                split_images_bytes_list, splitted_predictions, strict=True
            )
            if pred.strip().lower() not in ["multiple", "invalid"]
        ]

    # Stage 2: Extract and optionally upscale all valid images
    all_valid_images = single_images_valid + splitted_valid
    final_classifications = single_images_pred + splitted_pred

    # Classify video generation status early to skip upscaling if excluded
    video_gen_status = classify_video_gen_status(labels=final_classifications)

    if video_gen_status == "exclude":
        logger.info(
            f"Video generation status is 'exclude', skipping upscaling. Classifications: {final_classifications}"
        )
        return (
            list(zip(all_valid_images, final_classifications, strict=True)),
            video_gen_status,
            False,
        )

    # Velcro check before upscaling to skip expensive processing
    selected_closure_images = image_closure_selection(
        all_valid_images, final_classifications
    )
    has_velcro = False
    if selected_closure_images and len(selected_closure_images) > 0:
        closure_classification = classify_shoe_closure(
            selected_closure_images, client
        )
        has_velcro = closure_classification.get("has_velcro", False)
        if has_velcro:
            logger.info(
                "Velcro closure detected, skipping upscaling. Classifications: "
                f"{final_classifications}"
            )
            return (
                list(zip(all_valid_images, final_classifications, strict=True)),
                video_gen_status,
                True,
            )

    if upscale_images:
        # Extract, upscale, and clean
        all_valid_images = predict_parallel(
            all_valid_images,
            lambda img_bytes: extract_upscale_product(
                client, upscale_client, img_bytes, clean_after_upscale=True
            ),
            max_workers=num_workers,
            show_progress_bar=False,
        )

        # Check for upscaling failures
        if None in all_valid_images:
            none_count = all_valid_images.count(None)
            raise Exception(
                f"Image upscaling failed for {none_count} out of {len(all_valid_images)} images. "
                "The upscaling service may be temporarily unavailable. Please try again later."
            )
    else:
        # Just extract (remove background) without upscaling
        all_valid_images = predict_parallel(
            all_valid_images,
            lambda img_bytes: extract_product_from_background(
                client, img_bytes
            ),
            max_workers=num_workers,
            show_progress_bar=False,
        )

        # Check for extraction failures
        if None in all_valid_images:
            none_count = all_valid_images.count(None)
            raise Exception(
                f"Background extraction failed for {none_count} out of {len(all_valid_images)} images. "
                "The extraction service may be temporarily unavailable. Please try again later."
            )
    if create_canva:
        all_valid_images = create_canvas_with_height_scaling(
            images_bytes=all_valid_images
        )

    logger.info(f"Final classifications: {final_classifications}")

    return (
        list(zip(all_valid_images, final_classifications, strict=True)),
        video_gen_status,
        has_velcro,
    )


def run_video_gen_pipeline_r2v(
    images_bytes_list: list[bytes],
    client,
    veo_client,
    shoe_classifier_model: str,
    gemini_model: str,
    max_retries: int = 5,
    veo_model: str = "veo-3.1-generate-001",
    reference_type: str = "asset",
    product_consistency_model: str = os.getenv("MODEL_NAME_GENERATED_4"),
    product_id: str | None = None,
    gcs_bucket: str | None = None,
    gcs_destination_prefix: str = "shoe_spinning_outputs",
    gcs_project_id: str | None = None,
    upscale_images: bool = True,
    disable_logging: bool = True,
):
    """Reference-to-Video (R2V) pipeline that generates a single spinning video using reference images.

    This function:
    1. Classifies images to determine shoe positions and split multi-shoe images
    2. Extracts and optionally upscales images (no canvas creation)
    3. Checks if video generation is possible
    4. Stacks images to create up to 3 reference images
    5. Generates a single video using R2V modality

    Args:
        images_bytes_list: List of image bytes
        client: Gemini client for classification and extraction
        veo_client: Veo client for video generation
        shoe_classifier_model: Endpoint for shoe classification model
        gemini_model: Gemini model name for prompt generation
        max_retries: Maximum retries for video generation (default: 5)
        veo_model: Veo model to use for video generation (default: "veo-3.1-generate-001")
        reference_type: Type of reference images ("asset", "style", etc.) (default: "asset")
        product_consistency_model: Model for product consistency validation (default: "gemini-3.6-flash
        -preview")
        product_id: Product identifier for GCS uploads (required if gcs_bucket is set)
        gcs_bucket: GCS bucket name. If provided, uploads results to GCS (default: None)
        gcs_destination_prefix: Prefix in GCS bucket (default: "shoe_spinning_outputs")
        gcs_project_id: Optional GCP project ID for GCS uploads
        upscale_images: Whether to upscale images (default: True)
        disable_logging: If True, disables all logging output (default: True)

    Returns:
        bytes: Final video bytes if gcs_bucket is not provided
        dict: GCS metadata if gcs_bucket is provided, with keys:
            - video_gen_status: Status of video generation
            - clips: List containing single R2V clip metadata
            - gcs_uris: List of all uploaded file URIs
            - gcs_base_uri: Base GCS URI for the product
        None: On failure without gcs_bucket

    Raises:
        ValueError/Exception: If error occurs and gcs_bucket is set

    """
    # Save current logger level and disable if requested
    original_level = logger.level
    if disable_logging:
        logger.setLevel(logging.CRITICAL + 1)  # Disable all logging

    # For I/O-bound API calls, use more workers than CPU cores
    num_workers = min(32, max(16, multiprocessing.cpu_count() * 2))
    try:
        logger.info("=== R2V PIPELINE STARTED ===")
        logger.info(f"Input images: {len(images_bytes_list)}")

        # Stage 1: Images classification, splitting and upscaling (NO CANVAS)
        images_classified, video_gen_status, has_velcro = (
            preprocess_classify_images(
                images_bytes_list=images_bytes_list,
                client=client,
                upscale_client=veo_client,
                shoe_classifier_model=shoe_classifier_model,
                num_workers=num_workers,
                upscale_images=upscale_images,
                create_canva=False,  # No canvas for R2V
            )
        )

        if video_gen_status == "exclude":
            raise ValueError(
                f"Cannot generate video with these images. Status: {video_gen_status}"
            )

        if has_velcro:
            raise ValueError(
                "Cannot generate video for products with velcro closures"
            )

        images_bytes_list = [x[0] for x in images_classified]

        selected_ordered_images = pick_images_by_ordered_best_side(
            images_classified
        )

        # Check if we have enough images for video generation
        if not selected_ordered_images or len(selected_ordered_images) < 2:
            raise Exception(
                f"Insufficient images for video generation. Found {len(selected_ordered_images) if selected_ordered_images else 0} suitable images, but at least 2 are required. "
                "Please provide more images showing different angles of the product."
            )

        images_bytes = [x[0] for x in selected_ordered_images]

        ordered_classes = [x[1] for x in selected_ordered_images]
        logger.info(
            f"Selected {len(images_bytes)} ordered images: {ordered_classes}"
        )
        images_picked = [x[0] for x in selected_ordered_images]

        # images_bytes = create_variable_canvas(images_picked, ordered_classes)

        # # Stage 2: Stack reference images to max 3
        # logger.info("Stage 2: Preparing reference images...")
        # reference_images, stacked_classes = stack_image_pairs_canva(images_bytes,ordered_classes)

        reference_images, stacked_classes = stack_and_canvas_images(
            images_picked, ordered_classes
        )

        logger.info(
            f"Reference images prepared: {len(reference_images)} images"
        )

        # Stage 3: Generate single R2V video
        logger.info("Stage 3: Generating R2V video...")

        clip_result = generate_single_clip_r2v(
            clip_idx=0,
            reference_images=reference_images,
            veo_client=veo_client,
            max_retries=max_retries,
            veo_model=veo_model,
            client=client,
            gemini_model=gemini_model,
            reference_type=reference_type,
            shoe_classifier_model=shoe_classifier_model,
            reference_images_unstacked=images_picked,
            reference_labels_unstacked=ordered_classes,
            disable_logging=disable_logging,
            product_consistency_model=product_consistency_model,
        )

        video_bytes = clip_result["video_bytes"]
        original_video_bytes = clip_result["original_video_bytes"]
        frame_list = clip_result[
            "frame_list"
        ]  # Get frames from validation (already extracted)

        logger.info(
            f"✓ R2V video generated ({len(video_bytes):,} bytes, {len(video_bytes) / 1024 / 1024:.2f} MB)"
        )

        # Process frames: sample 50 and resize to target size using the utility function
        sampled_frames_resized = sample_and_process_frames(
            frame_list=frame_list,
            target_num_frames=50,
            target_size=(1000, 1000),
            initial_class="right",
            reference_images=images_picked,
            reference_labels=ordered_classes,
            client=client,
            gemini_model=gemini_model,
        )

        logger.info(
            f"✓ Processed {len(sampled_frames_resized)} frames (resized to 1000x1000)"
        )
        logger.info("=== R2V PIPELINE COMPLETE ===")
        logger.info(
            f"Final video size: {len(video_bytes):,} bytes ({len(video_bytes) / 1024 / 1024:.2f} MB)"
        )
        logger.info(f"Video status: {video_gen_status}")

        # Stage 4: Upload to GCS if bucket is provided
        if gcs_bucket:
            if not product_id:
                raise ValueError(
                    "product_id is required when gcs_bucket is provided"
                )

            logger.info("Stage 4: Uploading to GCS...")

            # Add ALL reference images to the clip result for R2V
            clip_result["reference_images_bytes"] = reference_images
            clip_result["reference_images_classifications"] = (
                stacked_classes  # Classifications for stacked images
            )
            clip_result["reference_images_unstacked_bytes"] = (
                images_bytes  # Save unstacked reference frames
            )
            clip_result["reference_images_unstacked_classifications"] = (
                ordered_classes  # Classifications for unstacked images
            )
            clip_result["start_position"] = "reference"
            clip_result["end_position"] = "reference"
            # For backwards compatibility with save_and_upload_to_gcs, also add start/end
            clip_result["start_frame_bytes"] = reference_images[0]
            clip_result["end_frame_bytes"] = reference_images[-1]

            # Build result for GCS upload
            # For R2V: single clip IS the final video (no merging needed)
            gcs_result = {
                "clips": [clip_result],
                "final_video": video_bytes,
                "original_video": original_video_bytes,
                "video_gen_status": video_gen_status,
            }

            uploaded_files = save_and_upload_to_gcs(
                result=gcs_result,
                product_id=product_id,
                bucket_name=gcs_bucket,
                gcs_destination_prefix=gcs_destination_prefix,
                project_id=gcs_project_id,
                pre_sampled_frames=sampled_frames_resized,  # Pass pre-extracted and processed frames
                image_format="jpg",
            )

            logger.info(
                f"✓ Uploaded {len(uploaded_files)} files to gs://{gcs_bucket}/{gcs_destination_prefix}/{product_id}/"
            )

            # Return GCS metadata for endpoint response (exclude large byte fields, but keep classifications)
            clip_metadata_clean = {
                k: v
                for k, v in clip_result.items()
                if k
                not in (
                    "video_bytes",
                    "start_frame_bytes",
                    "end_frame_bytes",
                    "reference_images_bytes",
                    "reference_images_unstacked_bytes",
                )
            }

            return {
                "video_gen_status": video_gen_status,
                "clips": [clip_metadata_clean],  # Single clip for R2V
                "gcs_uris": uploaded_files,
                "gcs_base_uri": f"gs://{gcs_bucket}/{gcs_destination_prefix}/{product_id}/",
            }
        else:
            # Return video bytes and frames when no GCS
            return {
                "video_bytes": video_bytes,
                "frames": sampled_frames_resized,
                "retry_count": clip_result["retry_count"],
            }

    except Exception as e:
        logger.error(f"R2V Pipeline failed: {e}", exc_info=True)
        # Always re-raise exceptions for proper HTTP error responses
        raise
    finally:
        # Restore original logger level
        logger.setLevel(original_level)
