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

"""Product Consistency Validation Module.

Validates that generated video frames match the reference product images.
Used during video generation to detect hallucinations and product inconsistencies.
"""

import json
import logging
import time

import cv2
import numpy as np
from google.genai import types
from google.genai.errors import ClientError
from httpx import RemoteProtocolError

# Project imports
from workflows.shared.video_utils import extract_frames_as_bytes_list

logger = logging.getLogger(__name__)


# ============================================================================
# SSIM
# ===========================================================================


def bytes_to_numpy(image_bytes: bytes) -> np.ndarray:
    """Convert image bytes to numpy array.

    Args:
        image_bytes: Image as bytes

    Returns:
        Numpy array (BGR format)

    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img_array = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img_array is None:
        raise ValueError("Failed to decode image from bytes")

    return img_array


def calculate_ssim(img1_bytes: bytes, img2_bytes: bytes) -> float:
    """Calculate SSIM similarity between two images.

    Args:
        img1_bytes: First image as bytes
        img2_bytes: Second image as bytes

    Returns:
        SSIM score (0-1, higher is more similar)

    """
    from skimage.metrics import structural_similarity as ssim

    # Convert to numpy arrays
    img1 = bytes_to_numpy(img1_bytes)
    img2 = bytes_to_numpy(img2_bytes)

    # Resize images to match dimensions if they differ
    if img1.shape != img2.shape:
        img2 = cv2.resize(
            img2,
            (img1.shape[1], img1.shape[0]),
            interpolation=cv2.INTER_LANCZOS4,
        )

    # Convert to grayscale for SSIM calculation
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

    # Calculate SSIM
    score, _ = ssim(gray1, gray2, full=True)

    return float(score)


# ============================================================================
# Label Forward-Filling
# ============================================================================


def forward_fill_labels(
    frame_labels: list[str], frame_indices: list[int]
) -> tuple[list[str], list[int]]:
    """Forward-fill null/None labels with the previous non-null label.

    All frames between two consecutive labeled frames get assigned the label of the first one.

    Args:
        frame_labels: List of labels (can contain None/null)
        frame_indices: List of frame indices corresponding to labels

    Returns:
        Tuple of (filled_labels, filled_indices) with all null labels filled

    Example:
        Input labels:  ["left", None, None, "right", None]
        Input indices: [0, 6, 12, 18, 24]
        Output labels: ["left", "left", "left", "right", "right"]
        Output indices: [0, 6, 12, 18, 24]

    """
    if not frame_labels:
        return [], []

    filled_labels = []
    filled_indices = []
    last_valid_label = None

    for idx, label in zip(frame_indices, frame_labels, strict=False):
        if label is not None and label:
            # Valid label found, update last_valid_label
            last_valid_label = label
            filled_labels.append(label)
            filled_indices.append(idx)
        # Null label, use last valid label if available
        elif last_valid_label is not None:
            filled_labels.append(last_valid_label)
            filled_indices.append(idx)
            # else: skip frames before the first valid label

    filled_count = len(filled_labels) - sum(
        1 for label in frame_labels if label is not None and label
    )

    logger.info(
        f"Forward-filled {filled_count} null labels from {len(frame_labels)} sampled frames"
    )
    logger.info(
        f"Result: {len(filled_labels)} frames with labels (expanded from {sum(1 for label in frame_labels if label)} labeled)"
    )

    return filled_labels, filled_indices


# ============================================================================
# Frame Matching
# ============================================================================


def find_best_matches_with_ssim(
    matched_pairs: list[dict],
    all_frames: list[bytes],
    reference_frames: list[bytes],
    offset_frames: int = 4,
) -> list[dict]:
    """Find the best matching frame for each reference using SSIM.

    Then pick 3 frames: best match, frame at -offset, and frame at +offset.

    Args:
        matched_pairs: List of matched pairs from match_frames_to_references()
        all_frames: List of all video frame bytes
        reference_frames: List of reference frame bytes
        offset_frames: Number of frames before/after best match to include (default: 4)

    Returns:
        List of frames to evaluate (3 per reference: best, best-offset, best+offset):
        [
            {
                'frame_index': int,
                'label': str,
                'reference_index': int,
                'ssim_score': float,  # Only for best match
                'is_best_match': bool,
                'offset_type': str  # 'best', 'before', or 'after'
            }
        ]

    """
    logger.info("Finding best matches using SSIM for each reference...")

    # Group matched pairs by reference
    pairs_by_reference = {}
    for pair in matched_pairs:
        ref_idx = pair["reference_index"]
        if ref_idx not in pairs_by_reference:
            pairs_by_reference[ref_idx] = []
        pairs_by_reference[ref_idx].append(pair)

    logger.info(
        f"Grouped {len(matched_pairs)} matched pairs into {len(pairs_by_reference)} reference groups"
    )

    # Find best match for each reference
    frames_to_evaluate = []

    for ref_idx, pairs in pairs_by_reference.items():
        logger.info(
            f"  Reference {ref_idx}: Comparing {len(pairs)} candidate frames..."
        )

        # Calculate SSIM for all candidate frames
        ssim_scores = []
        for pair in pairs:
            frame_idx = pair["frame_index"]
            gen_frame_bytes = all_frames[frame_idx]
            ref_frame_bytes = reference_frames[ref_idx]

            ssim_score = calculate_ssim(gen_frame_bytes, ref_frame_bytes)
            ssim_scores.append({"pair": pair, "ssim_score": ssim_score})

        # Find best match (highest SSIM)
        best = max(ssim_scores, key=lambda x: x["ssim_score"])
        best_match_idx = best["pair"]["frame_index"]
        best_score = best["ssim_score"]

        logger.info(
            f"    Best match: frame {best_match_idx} (SSIM: {best_score:.4f})"
        )

        # Pick 3 frames: best, best-offset, best+offset
        frame_before_idx = best_match_idx - offset_frames
        frame_after_idx = best_match_idx + offset_frames

        # Add best match
        frames_to_evaluate.append(
            {
                "frame_index": best_match_idx,
                "label": best["pair"]["frame_label"],
                "reference_index": ref_idx,
                "ssim_score": best_score,
                "is_best_match": True,
                "offset_type": "best",
            }
        )

        # Add frame before (if valid)
        if frame_before_idx >= 0:
            frames_to_evaluate.append(
                {
                    "frame_index": frame_before_idx,
                    "label": best["pair"]["frame_label"],
                    "reference_index": ref_idx,
                    "ssim_score": None,
                    "is_best_match": False,
                    "offset_type": "before",
                }
            )
            logger.info(
                f"    Frame before: frame {frame_before_idx} (offset: -{offset_frames})"
            )
        else:
            logger.info(
                f"    Frame before: skipped (index {frame_before_idx} < 0)"
            )

        # Add frame after (if valid)
        if frame_after_idx < len(all_frames):
            frames_to_evaluate.append(
                {
                    "frame_index": frame_after_idx,
                    "label": best["pair"]["frame_label"],
                    "reference_index": ref_idx,
                    "ssim_score": None,
                    "is_best_match": False,
                    "offset_type": "after",
                }
            )
            logger.info(
                f"    Frame after: frame {frame_after_idx} (offset: +{offset_frames})"
            )
        else:
            logger.info(
                f"    Frame after: skipped (index {frame_after_idx} >= {len(all_frames)})"
            )

    # Remove duplicates (frames might appear for multiple references)
    seen = set()
    unique_frames = []
    for frame in frames_to_evaluate:
        key = (frame["frame_index"], frame["reference_index"])
        if key not in seen:
            seen.add(key)
            unique_frames.append(frame)

    logger.info(
        f"Total frames to evaluate: {len(unique_frames)} (max 3 per reference)"
    )

    # Sort by frame index for ordered evaluation
    unique_frames.sort(key=lambda x: x["frame_index"])

    return unique_frames


def match_frames_to_references(
    frame_labels: list[str],
    frame_indices: list[int],
    reference_labels: list[str],
) -> list[dict]:
    """Match generated frame labels to reference image labels.

    Args:
        frame_labels: List of predicted labels for generated frames
        frame_indices: List of frame indices corresponding to labels
        reference_labels: List of labels for reference images

    Returns:
        List of matched pairs:
        [
            {
                'frame_index': int,
                'frame_label': str,
                'reference_index': int,
                'reference_label': str
            }
        ]

    """
    # Build reference lookup: label -> first reference index with that label
    reference_lookup = {}
    for ref_idx, label in enumerate(reference_labels):
        if label and label not in reference_lookup:
            reference_lookup[label] = ref_idx

    logger.info(f"Reference labels available: {list(reference_lookup.keys())}")

    matched_pairs = []
    skipped = 0

    for frame_idx, frame_label in zip(
        frame_indices, frame_labels, strict=False
    ):
        # Skip null/empty labels
        if not frame_label or frame_label.strip() == "":
            skipped += 1
            continue

        clean_label = frame_label.strip()

        # Check if label exists in references
        if clean_label in reference_lookup:
            matched_pairs.append(
                {
                    "frame_index": frame_idx,
                    "frame_label": clean_label,
                    "reference_index": reference_lookup[clean_label],
                    "reference_label": clean_label,
                }
            )
        else:
            logger.info(
                f"Frame {frame_idx} has label '{clean_label}' not found in references"
            )
            skipped += 1

    if skipped > 0:
        logger.info(f"Skipped {skipped} frames (no matching reference)")

    logger.info(f"Matched {len(matched_pairs)} frame-reference pairs")

    return matched_pairs


# ============================================================================
# Evaluation Prompt and LLM Evaluation
# ============================================================================

MULTI_VIEW_EVAL_PROMPT = """
You are an AI expert in evaluating product image consistency across multiple views.

You will receive:
1. REFERENCE IMAGES: Multiple views of the product (ground truth) - each labeled with its view angle
2. GENERATED IMAGES: Corresponding generated views - each labeled with its view angle

Your task is to determine if the GENERATED images maintain product consistency with the REFERENCE images.

**Evaluation Process:**
- Compare each generated view against its corresponding reference view
- Look across ALL views to ensure the product details are consistent
- A SINGLE **major structural** inconsistency in ANY view makes the entire generation INVALID

**Focus on (Strict Criteria):**
*   **Feature Placement:** Verify that logos, text blocks, stripes, and patterns appear in the correct location and color relative to the reference.
*   **Structural Integrity:** Ensure the main silhouette and shape components match the reference.
*   **Color Consistency:** Major color blocking and material appearance must match.
*   **Hallucinations:** Flag major objects or distinct patterns present in generated images that are completely absent in the reference.

**Ignore (Lenient Criteria):**
*   **Text Legibility/Spelling:** **DO NOT** attempt to read the text. If a text block is present in the correct position and color, it is VALID, even if the text is blurry, gibberish, misspelled, or illegible "squiggles."
*   **Logo Fidelity:** If a logo shape is present but lacks internal detail, sharpness, or looks "melted," it is acceptable.
*   **Generative Artifacts:** Minor pixilation, slight warping of small details, or lack of fine texture resolution.
*   **Light/Environment:** Reflections, shadows, brightness, and background differences.
*   **Minor Angle Variations:** Generated images may be intermediate views between reference angles.

**IMPORTANT:**
- **Treat text and logos as "Visual Blobs":** If a patch of color/pattern representing a logo or text exists in the correct spot, mark it as consistent. Do not penalize for low resolution or lack of readability.
- Focus ONLY on the product details shown in the references.
- Ignore your knowledge about brands (e.g., if a Nike logo is backward but visually present, it is acceptable if consistent with the generation style).
- Use ONLY the provided reference images as ground truth.

Return a single evaluation for ALL views:

Output format (JSON):
{
  "is_valid": true if ALL generated views are consistent and valid, false if ANY view has issues,
  "explanation": "detailed reasoning mentioning which specific views have issues if invalid"
}
"""


def evaluate_all_views_single_call(
    client,
    frames_to_evaluate: list[dict],
    all_frame_bytes: list[bytes],
    reference_images_bytes: list[bytes],
    reference_labels: list[str],
    eval_prompt: str,
    model: str,
) -> dict:
    """Evaluate ALL reference and generated views in a SINGLE API call.

    This provides a multi-view evaluation where:
    - All reference images are shown with their view labels
    - All generated images are shown with their view labels
    - A single true/false decision is returned for the entire product

    Args:
        client: Gemini client
        frames_to_evaluate: List of frame dicts from find_best_matches_with_ssim()
        all_frame_bytes: List of all generated frame bytes
        reference_images_bytes: List of reference image bytes
        reference_labels: List of reference labels (e.g., ['right', 'left', 'front_right'])
        eval_prompt: Evaluation prompt (should be MULTI_VIEW_EVAL_PROMPT)
        model: Gemini model name

    Returns:
        Dict with 'is_valid' (bool) and 'explanation' (str)

    """
    logger.info("Evaluating all views in a single API call...")
    logger.info(f"  References: {len(reference_images_bytes)} views")
    logger.info(f"  Generated: {len(frames_to_evaluate)} frames")

    # Build the parts list
    parts = [types.Part.from_text(text=eval_prompt)]

    # Group frames by reference to organize the input
    frames_by_ref = {}
    for frame in frames_to_evaluate:
        ref_idx = frame["reference_index"]
        if ref_idx not in frames_by_ref:
            frames_by_ref[ref_idx] = []
        frames_by_ref[ref_idx].append(frame)

    # Section 1: Add ALL reference images
    parts.append(types.Part.from_text(text="\n" + "=" * 10))
    parts.append(types.Part.from_text(text="REFERENCE IMAGES (Ground Truth)"))
    parts.append(types.Part.from_text(text="=" * 10))

    for ref_idx in sorted(frames_by_ref.keys()):
        ref_label = reference_labels[ref_idx]
        parts.append(
            types.Part.from_text(text=f"\nReference View: {ref_label.upper()}")
        )
        parts.append(
            types.Part.from_bytes(
                data=reference_images_bytes[ref_idx], mime_type="image/png"
            )
        )

    # Section 2: Add ALL generated images
    parts.append(types.Part.from_text(text="\n" + "=" * 10))
    parts.append(types.Part.from_text(text="GENERATED IMAGES (To Evaluate)"))
    parts.append(types.Part.from_text(text="=" * 10))

    for ref_idx in sorted(frames_by_ref.keys()):
        ref_label = reference_labels[ref_idx]
        frames = sorted(frames_by_ref[ref_idx], key=lambda x: x["frame_index"])

        # Group header for this view
        parts.append(
            types.Part.from_text(
                text=f"\nGenerated views for: {ref_label.upper()}"
            )
        )

        for frame in frames:
            frame_idx = frame["frame_index"]
            offset_type = frame["offset_type"]
            ssim_score = frame.get("ssim_score")

            # Create descriptive label
            desc = f"  • Frame {frame_idx} ({offset_type})"
            if ssim_score is not None:
                desc += f" [SSIM: {ssim_score:.4f}]"

            parts.append(types.Part.from_text(text=desc))
            parts.append(
                types.Part.from_bytes(
                    data=all_frame_bytes[frame_idx], mime_type="image/png"
                )
            )

    # Add final instruction
    parts.append(types.Part.from_text(text="\n" + "=" * 10))
    parts.append(
        types.Part.from_text(
            text="Evaluate ALL views above and return a SINGLE verdict (is_valid: true/false)"
        )
    )
    parts.append(types.Part.from_text(text="=" * 10))

    # Make the API call with retry logic
    logger.info(f"Sending request to {model}...")

    sleep_time = 0
    retry_num = -1

    while True:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    temperature=0,  # For stability
                    response_mime_type="application/json",
                    response_modalities=["TEXT"],
                ),
            )

            result = json.loads(response.text)
            is_valid = result.get("is_valid", False)

            logger.info(
                f"Evaluation complete: {'VALID' if is_valid else 'INVALID'}"
            )

            return result

        except (ClientError, RemoteProtocolError) as e:
            if hasattr(e, "code") and e.code in (401, 403, 404):
                logger.error(
                    f"Non-retryable error ({e.code}) in evaluate_all_views_single_call: {e}"
                )
                raise
            sleep_time += 10
            retry_num += 1
            time.sleep(sleep_time)
            if retry_num >= 100:
                logger.error(
                    f"Max retries (100) reached for evaluate_all_views_single_call: {e}"
                )
                raise
            logger.error(
                f"Error calling generate_content: {e}, waiting: {sleep_time}s (retry {retry_num}/100)"
            )


# ============================================================================
# Product consistency eval
# ============================================================================


def validate_product_consistency(
    video_bytes: bytes,
    frame_classifications: list[str],
    sampled_indices: list[int],
    reference_images_bytes: list[bytes],
    reference_labels: list[str],
    client,
    model: str,
    eval_prompt: str = MULTI_VIEW_EVAL_PROMPT,
    forward_fill: bool = True,
    use_ssim_selection: bool = True,
    offset_frames: int = 4,
) -> tuple[bool, str, dict]:
    """Validate product consistency between generated video frames and reference images.

    New multi-view approach:
    1. Extracts only the sampled frames using indices (reuses sampling from eval_product_spin_consistency_r2v)
    2. Forward-fills null labels to expand coverage
    3. Matches frames to references by label
    4. Uses SSIM to find best match for each reference + window
    5. Evaluates ALL views in a SINGLE API call (multi-view consistency)
    6. Returns overall validation result

    Args:
        video_bytes: Generated video as bytes
        frame_classifications: Labels for sampled frames (from eval_product_spin_consistency_r2v)
        sampled_indices: Indices of sampled frames (from eval_product_spin_consistency_r2v)
        reference_images_bytes: List of reference image bytes (unstacked)
        reference_labels: List of labels for reference images (unstacked)
        client: Gemini client location needs to be global for gemini 3 in future it may be changed
        model: Gemini model name using gemini 3 pro
        eval_prompt: Evaluation prompt (default: MULTI_VIEW_EVAL_PROMPT)
        forward_fill: If True, forward-fill null labels (default: True)
        use_ssim_selection: If True, use SSIM to find best matches + offsets (default: True)
        offset_frames: Number of frames before/after best SSIM match to include (default: 4)

    Returns:
        Tuple of (is_valid, message, evaluation_result)
        - is_valid: True if all views are valid, False otherwise
        - message: Summary message
        - evaluation_result: Dict with 'is_valid' and 'explanation' for the entire product

    """
    logger.info("=== Product Consistency Validation Started ===")

    # Step 1: Extract all frames and get only the sampled ones using indices
    all_frames = extract_frames_as_bytes_list(video_bytes)
    logger.info(f"Extracted {len(all_frames)} frames from video")

    # Get only the sampled frames using the indices from eval_product_spin_consistency_r2v
    sampled_frames = [all_frames[idx] for idx in sampled_indices]
    logger.info(
        f"Using {len(sampled_frames)} sampled frames (indices from spin validation)"
    )

    # Step 2: Forward-fill labels if requested
    if forward_fill:
        filled_labels, filled_indices = forward_fill_labels(
            frame_labels=frame_classifications, frame_indices=sampled_indices
        )
    else:
        filled_labels = frame_classifications
        filled_indices = sampled_indices

    # Step 3: Match frames to references
    matched_pairs = match_frames_to_references(
        frame_labels=filled_labels,
        frame_indices=filled_indices,
        reference_labels=reference_labels,
    )

    if not matched_pairs:
        logger.warning("No frames matched to references - cannot validate")
        return (
            True,
            "No frames to validate (no matches found)",
            {"is_valid": True, "explanation": "No frames to validate"},
        )

    # Step 4: (Optional) Use SSIM to select best matches + offsets
    if use_ssim_selection:
        logger.info(
            f"Using SSIM-based selection with offset ±{offset_frames} frames"
        )
        frames_to_evaluate = find_best_matches_with_ssim(
            matched_pairs=matched_pairs,
            all_frames=all_frames,
            reference_frames=reference_images_bytes,
            offset_frames=offset_frames,
        )
    else:
        # Use all matched pairs
        frames_to_evaluate = matched_pairs

    if not frames_to_evaluate:
        logger.warning("No frames selected for evaluation")
        return (
            True,
            "No frames to evaluate (SSIM selection returned empty)",
            {"is_valid": True, "explanation": "No frames selected"},
        )

    # Step 5: Evaluate ALL views in a SINGLE API call
    logger.info(
        f"Evaluating {len(frames_to_evaluate)} frames across {len(reference_images_bytes)} references..."
    )

    result = evaluate_all_views_single_call(
        client=client,
        frames_to_evaluate=frames_to_evaluate,
        all_frame_bytes=all_frames,
        reference_images_bytes=reference_images_bytes,
        reference_labels=reference_labels,
        eval_prompt=eval_prompt,
        model=model,
    )

    # Step 6: Compute validation summary
    is_valid = result.get("is_valid", False)
    explanation = result.get("explanation", "")

    message = (
        f"Product consistency validation: {'VALID' if is_valid else 'INVALID'} "
        f"({len(frames_to_evaluate)} frames evaluated across {len(reference_images_bytes)} references)"
    )

    if not is_valid:
        logger.warning(f"{message}")
        logger.warning(f"  Explanation: {explanation[:200]}...")
    else:
        logger.info(f"{message}")

    logger.info("=== Product Consistency Validation Complete ===")

    return is_valid, message, result
