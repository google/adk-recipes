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

"""Person/face evaluation utilities for comparing faces in generated images.

Used by VTO (Virtual Try-On) and Background Changer features.
"""

# Standard library imports
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# Third-party imports
import cv2
import numpy as np
from insightface.app import FaceAnalysis
from PIL import Image

# Project imports
from workflows.shared.image_utils import crop_face

logger = logging.getLogger(__name__)

# Module-level thread pool for evaluation
_eval_pool = None
_POOL_MAX_WORKERS = 4

# Module-level InsightFace app (CPU-based, initialized lazily)
_insightface_app = None


def _now():
    """Return current time as HH:MM:SS.mmm."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def get_insightface_app() -> FaceAnalysis:
    """Get or create the InsightFace FaceAnalysis app.

    Uses buffalo_l model for highest accuracy, running on CPU via ONNX.
    """
    global _insightface_app
    if _insightface_app is None:
        logger.info("[InsightFace] Initializing FaceAnalysis app (CPU mode)...")
        _insightface_app = FaceAnalysis(
            name="buffalo_l", providers=["CPUExecutionProvider"]
        )
        _insightface_app.prepare(ctx_id=-1, det_size=(640, 640))
        logger.info("[InsightFace] FaceAnalysis app initialized successfully")
    return _insightface_app


def _mask_eye_regions(img_bgr: np.ndarray, face) -> np.ndarray:
    """Mask both eye regions with skin-colored ellipses.

    Uses InsightFace's 5-point keypoints (left_eye, right_eye, nose,
    mouth_left, mouth_right) to compute ellipse size from inter-eye distance,
    samples skin color from the cheek area below each eye, and draws filled
    ellipses over the eyes.

    Args:
        img_bgr: BGR image as numpy array (will NOT be modified in-place).
        face: InsightFace detected face object with .kps attribute.

    Returns:
        A new BGR image with eye regions masked.

    """
    masked = img_bgr.copy()
    kps = face.kps  # shape (5, 2): left_eye, right_eye, nose, mouth_l, mouth_r

    left_eye = kps[0].astype(int)  # (x, y)
    right_eye = kps[1].astype(int)

    inter_eye_dist = np.linalg.norm(kps[1] - kps[0])
    ellipse_w = int(inter_eye_dist * 0.40)
    ellipse_h = int(inter_eye_dist * 0.25)

    h, w = masked.shape[:2]

    for eye_pt in [left_eye, right_eye]:
        # Sample skin color from cheek area (below the eye)
        sample_y = min(eye_pt[1] + ellipse_h + 5, h - 1)
        sample_x = np.clip(eye_pt[0], 0, w - 1)
        # Average a small patch for robustness
        y_lo = max(sample_y - 2, 0)
        y_hi = min(sample_y + 3, h)
        x_lo = max(sample_x - 2, 0)
        x_hi = min(sample_x + 3, w)
        patch = masked[y_lo:y_hi, x_lo:x_hi]
        skin_color = patch.mean(axis=(0, 1)).astype(int).tolist()

        center = (int(eye_pt[0]), int(eye_pt[1]))
        axes = (ellipse_w, ellipse_h)
        cv2.ellipse(
            masked,
            center,
            axes,
            angle=0,
            startAngle=0,
            endAngle=360,
            color=skin_color,
            thickness=-1,
        )

    return masked


def get_eval_pool() -> ThreadPoolExecutor:
    """Get or create the shared evaluation thread pool."""
    global _eval_pool
    if _eval_pool is None:
        logger.info(
            f"[Face Eval] Creating thread pool with {_POOL_MAX_WORKERS} workers"
        )
        _eval_pool = ThreadPoolExecutor(max_workers=_POOL_MAX_WORKERS)
    return _eval_pool


def validate_model_photo(
    img_bytes: bytes, max_yaw: float = 30.0, max_pitch: float = 25.0
) -> dict:
    """Validate that the uploaded model photo shows a person looking at the camera.

    Uses InsightFace pose estimation and eye aspect ratio to ensure eyes are open.

    Args:
        img_bytes: Image as bytes.
        max_yaw: Maximum allowed horizontal head rotation in degrees.
        max_pitch: Maximum allowed vertical head rotation in degrees.

    Returns:
        dict: {"valid": bool, "reason": str or None}

    """
    app = get_insightface_app()

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_np = np.array(img)
    img_bgr = img_np[:, :, ::-1]

    faces = app.get(img_bgr)
    if not faces:
        return {
            "valid": False,
            "reason": "No face detected. Please upload a clear frontal photo of a person looking at the camera.",
        }

    face = faces[0]

    # Check head pose (pose is [pitch, yaw, roll] in degrees)
    if hasattr(face, "pose"):
        pitch, yaw, roll = face.pose
        logger.info(
            f"[Photo Validation] Head pose — yaw: {yaw:.1f}, pitch: {pitch:.1f}, roll: {roll:.1f}"
        )
        if abs(yaw) > max_yaw or abs(pitch) > max_pitch:
            return {
                "valid": False,
                "reason": "The person is not looking at the camera. Please upload a frontal photo where the person faces the camera directly.",
            }

    # Check eyes open using 3D 68-point landmarks (Eye Aspect Ratio)
    if hasattr(face, "landmark_3d_68") and face.landmark_3d_68 is not None:
        lm = face.landmark_3d_68  # shape (68, 3) or (68, 2)

        # Left eye: points 36-41, Right eye: points 42-47
        def _ear(eye_pts):
            v1 = np.linalg.norm(eye_pts[1] - eye_pts[5])
            v2 = np.linalg.norm(eye_pts[2] - eye_pts[4])
            h = np.linalg.norm(eye_pts[0] - eye_pts[3])
            return (v1 + v2) / (2.0 * h) if h > 0 else 0

        left_ear = _ear(lm[36:42])
        right_ear = _ear(lm[42:48])
        avg_ear = (left_ear + right_ear) / 2.0
        logger.info(
            f"[Photo Validation] Eye Aspect Ratio — left: {left_ear:.3f}, right: {right_ear:.3f}, avg: {avg_ear:.3f}"
        )

        if avg_ear < 0.18:
            return {
                "valid": False,
                "reason": "The person's eyes appear to be closed. Please upload a photo where the person has their eyes open and is looking at the camera.",
            }

    logger.info("[Photo Validation] Model photo passed validation")
    return {"valid": True, "reason": None}


def get_face_embedding_insightface(
    img_bytes: bytes, mask_eyes: bool = False
) -> np.ndarray | None:
    """Extract face embedding from an image using InsightFace.

    Args:
        img_bytes: Image as bytes.
        mask_eyes: If True, mask the eye regions before extracting the
            embedding so that ArcFace compares only occlusion-invariant
            features (nose, mouth, jaw, forehead).

    Returns:
        np.ndarray: 512-dimensional normalized face embedding, or None if no face detected

    """
    app = get_insightface_app()

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_np = np.array(img)
    img_bgr = img_np[:, :, ::-1]

    faces = app.get(img_bgr)

    if not faces:
        logger.warning("[InsightFace] No face detected in image")
        return None

    if mask_eyes:
        logger.debug(
            "[InsightFace] Masking eye regions before embedding extraction"
        )
        masked_bgr = _mask_eye_regions(img_bgr, faces[0])
        masked_faces = app.get(masked_bgr)
        if masked_faces:
            embedding = masked_faces[0].normed_embedding
            logger.debug(
                f"[InsightFace] Extracted masked embedding shape: {embedding.shape}"
            )
            return embedding
        logger.warning(
            "[InsightFace] No face detected after masking eyes, falling back to unmasked"
        )

    embedding = faces[0].normed_embedding
    logger.debug(f"[InsightFace] Extracted embedding shape: {embedding.shape}")
    return embedding


def compare_faces_insightface(
    reference_face_bytes: bytes,
    generated_face_bytes: bytes,
    mask_eyes: bool = False,
) -> dict:
    """Compare two face images using InsightFace embeddings.

    Args:
        reference_face_bytes: Reference face image as bytes.
        generated_face_bytes: Generated face image as bytes.
        mask_eyes: If True, mask eye regions before extracting embeddings.

    Returns:
        dict: {
            "similarity_percentage": float (0-100),
            "distance": float (cosine distance, 0-2),
            "model": str,
            "embeddings_extracted": bool
        }

    """
    logger.info(f"[InsightFace] Comparing faces [{_now()}]")

    ref_embedding = get_face_embedding_insightface(
        reference_face_bytes, mask_eyes=mask_eyes
    )
    gen_embedding = get_face_embedding_insightface(
        generated_face_bytes, mask_eyes=mask_eyes
    )

    if ref_embedding is None or gen_embedding is None:
        logger.warning(
            "[InsightFace] Could not extract embeddings from one or both images"
        )
        return {
            "similarity_percentage": 0.0,
            "distance": 2.0,
            "model": "InsightFace-ArcFace",
            "embeddings_extracted": False,
        }

    cosine_similarity = np.dot(ref_embedding, gen_embedding)
    cosine_distance = 1 - cosine_similarity
    similarity_percentage = max(0, min(100, cosine_similarity * 100))

    logger.info(
        f"[InsightFace] Cosine similarity: {cosine_similarity:.4f}, "
        f"Distance: {cosine_distance:.4f}, "
        f"Similarity: {similarity_percentage:.2f}%"
    )

    return {
        "similarity_percentage": float(similarity_percentage),
        "distance": float(cosine_distance),
        "model": "InsightFace-ArcFace",
        "embeddings_extracted": True,
    }


def evaluate_person_match(
    reference_face_bytes, generated_vto_bytes, mask_eyes=False
):
    """Evaluate if the person in a generated image matches the reference face.

    This is the main function to use. It handles:
    1. Cropping face from generated image
    2. Comparing with preprocessed reference face
    3. Returning similarity metrics

    Args:
        reference_face_bytes (bytes): Reference face ALREADY cropped+upscaled
        generated_vto_bytes (bytes): Generated image as bytes
        mask_eyes (bool): If True, mask eye regions before comparing embeddings.

    Returns:
        dict: {
            "similarity_percentage": float,
            "distance": float,
            "model": str,
            "face_detected": bool
        }

    """
    logger.info(f"[InsightFace Eval] Starting person evaluation [{_now()}]")

    generated_face_bytes = crop_face(generated_vto_bytes)

    if generated_face_bytes is None:
        logger.warning("[InsightFace Eval] No face detected in generated image")
        return {
            "similarity_percentage": 0.0,
            "distance": 2.0,
            "model": "InsightFace-ArcFace",
            "face_detected": False,
        }

    logger.info(f"[InsightFace Eval] Comparing faces [{_now()}]")

    try:
        result = compare_faces_insightface(
            reference_face_bytes, generated_face_bytes, mask_eyes=mask_eyes
        )
        result["face_detected"] = result.get("embeddings_extracted", False)

        similarity = result["similarity_percentage"]
        logger.info(
            f"[InsightFace Eval] Evaluation complete - Similarity: {similarity:.2f}% [{_now()}]"
        )

        return result

    except Exception as e:
        logger.error(f"[InsightFace Eval] Failed to evaluate person match: {e}")
        return {
            "similarity_percentage": 0.0,
            "distance": 2.0,
            "model": "InsightFace-ArcFace",
            "face_detected": False,
            "error": str(e),
        }


def submit_evaluation(
    reference_face_bytes: bytes,
    generated_vto_bytes: bytes,
    mask_eyes: bool = False,
):
    """Submit an evaluation task to the shared thread pool.

    Args:
        reference_face_bytes: Reference face image as bytes.
        generated_vto_bytes: Generated VTO image as bytes.
        mask_eyes: If True, mask eye regions before comparing embeddings.

    Returns:
        Future: A Future that resolves to the evaluation result dict

    """
    pool = get_eval_pool()
    return pool.submit(
        evaluate_person_match,
        reference_face_bytes,
        generated_vto_bytes,
        mask_eyes,
    )
