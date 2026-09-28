# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Glasses Rotation Classifier - Mask-based shape analysis for symmetric objects.

For glasses and other symmetric objects, optical flow is unreliable because left-right
symmetry makes flow direction ambiguous. Instead, this classifier:
1. Extracts foreground masks from video frames using segmentation
2. Tracks the horizontal centroid shift of the mask over time
3. Determines rotation direction from the centroid movement pattern

Returns: "clockwise", "anticlockwise", or "invalid"
"""

import io
import logging
import os
import tempfile

import cv2
import numpy as np
from PIL import Image as PImage
from workflows.shared.image_utils import get_background_mask_vertex

logger = logging.getLogger(__name__)

# Sample every Nth frame for efficiency
SAMPLE_INTERVAL = 4
MIN_MASK_AREA_RATIO = 0.005  # Minimum mask area as fraction of frame


def classify_glasses_rotation(
    segmentation_client,
    video_bytes: bytes,
) -> str:
    """Classify rotation direction for glasses/symmetric objects using mask centroids.

    Args:
        segmentation_client: Gemini client for image segmentation.
        video_bytes: Video file as bytes.

    Returns:
        "clockwise", "anticlockwise", or "invalid"

    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            logger.error("[Glasses Rotation] Could not open video")
            return "invalid"

        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

        centroids_x = []
        frame_idx = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % SAMPLE_INTERVAL == 0:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_img = PImage.fromarray(frame_rgb)
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                frame_bytes = buf.getvalue()

                try:
                    mask_bytes = get_background_mask_vertex(
                        segmentation_client, frame_bytes
                    )
                    mask_img = PImage.open(io.BytesIO(mask_bytes)).convert("L")
                    mask_np = np.array(mask_img)

                    if mask_np.shape[:2] != frame.shape[:2]:
                        mask_np = cv2.resize(
                            mask_np, (frame.shape[1], frame.shape[0])
                        )

                    mask_area = np.sum(mask_np > 128)
                    total_area = mask_np.shape[0] * mask_np.shape[1]

                    if mask_area / total_area > MIN_MASK_AREA_RATIO:
                        _ys, xs = np.where(mask_np > 128)
                        cx = float(np.mean(xs))
                        centroids_x.append(cx)
                except Exception as e:
                    logger.debug(
                        f"[Glasses Rotation] Skipping frame {frame_idx}: {e}"
                    )

            frame_idx += 1

        cap.release()
        os.unlink(tmp_path)

        if len(centroids_x) < 6:
            logger.warning(
                f"[Glasses Rotation] Too few valid frames ({len(centroids_x)})"
            )
            return "invalid"

        center_x = frame_width / 2.0
        normalized = [cx - center_x for cx in centroids_x]
        dxs = [
            normalized[i + 1] - normalized[i]
            for i in range(len(normalized) - 1)
        ]

        cw_score = 0.0
        acw_score = 0.0

        for i in range(len(dxs)):
            pos = normalized[i]
            dx = dxs[i]

            if abs(pos) > frame_width * 0.02 and abs(dx) > 0.5:
                if (pos > 0 and dx < 0) or (pos < 0 and dx > 0):
                    cw_score += abs(dx)
                elif (pos > 0 and dx > 0) or (pos < 0 and dx < 0):
                    acw_score += abs(dx)

        total = cw_score + acw_score
        if total < 1.0:
            logger.info("[Glasses Rotation] Insufficient centroid movement")
            return "invalid"

        ratio = max(cw_score, acw_score) / total
        if ratio < 0.6:
            logger.info(
                f"[Glasses Rotation] Ambiguous: cw={cw_score:.1f}, acw={acw_score:.1f}"
            )
            return "invalid"

        direction = "clockwise" if cw_score > acw_score else "anticlockwise"
        logger.info(
            f"[Glasses Rotation] {direction} (cw={cw_score:.1f}, acw={acw_score:.1f}, ratio={ratio:.2f})"
        )
        return direction

    except Exception as e:
        logger.error(f"[Glasses Rotation] Classification failed: {e}")
        return "invalid"
