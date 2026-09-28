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

"""Local shoe classifier using Gemini embeddings + a numpy neural network.

Uses a V2 primary model (5 views + 27 features) with a V1 fallback model
(4 views) to guard against false "invalid" predictions on OOD images.
When V2 predicts "invalid" but V1 disagrees with >=99% confidence,
V1's prediction is used instead.

Loaded once at import time (module-level singleton). Falls back to this
when SHOE_CLASSIFICATION_ENDPOINT is not set.
"""

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from google import genai
from PIL import Image
from workflows.shared.gemini import embed_gemini
from workflows.shared.utils import predict_parallel

from app.config import BACKEND_ASSETS_DIR

logger = logging.getLogger(__name__)

CLASSES = [
    "front",
    "front_right",
    "front_left",
    "top_front",
    "right",
    "left",
    "back",
    "back_right",
    "back_left",
    "sole",
    "invalid",
    "multiple",
]
_INVALID_IDX = CLASSES.index("invalid")
_V1_OVERRIDE_CONF = 0.99

_SHOES_ASSETS = BACKEND_ASSETS_DIR / "spinning" / "r2v" / "shoes"
_WEIGHTS_V2_PATH = str(_SHOES_ASSETS / "shoe_classifier_numpy_v2.npz")
_WEIGHTS_V1_PATH = str(_SHOES_ASSETS / "shoe_classifier_numpy.npz")
_SYSTEM_TEXT = """\
You are an **expert in footwear and image understanding**. Your primary role is to **analyze the user-provided image** and **classify the position (viewpoint and orientation) of the footwear product** within it.

The product's position must be classified into one of the following distinct categories:
* **front:** The product is in a **perfect eye-level, front-facing position** relative to the camera (i.e., the toe is directly facing the camera).
* **front_right:** The product front and right end side are prominent and visible. Ie. It is possible to understand how both the right side and the front side of the product look like from the photo.
* **front_left:** The product front and left end side are prominent and visible. Ie. It is possible to understand how both the front side and the left side of the product look like from the photo.
* **top_front:** The product is in a **front-facing position** where the camera is clearly **above the product**, capturing a view from the top down (i.e. the top of the shoe is prominent).
* **back:** The product is in a **perfect back-facing position** relative to the camera (i.e., the **heel** is directly facing the camera).
* **back_right:** The product back and right end side are prominent and visible. Ie. It is possible to understand how both the back side and the right side of the product look like from the photo.
* **back_left:** The product back and left end side are prominent and visible. Ie. It is possible to understand how both the back side and the left side of the product look like from the photo.
* **right:** The product is **rotated to the right**. Both the toe and the heel are visible, and the **toe is on the right** side of the image relative to the heel.
* **left:** The product is **rotated to the left**. Both the toe and the heel are visible, and the **toe is on the left** side of the image relative to the heel.
* **sole:** The product is **upside down**, and the **sole** is the dominant feature, filling the majority of the image frame.
* **multiple:** The image contains **two or more** distinct footwear items (e.g., shoes, boots, sandals, etc.). This includes cases where the items are a matching pair, the items are unmatched and one or more item is partially obscured.
* **invalid:** Return `invalid` in all other cases including:
    * The image contains no footwear.
    * The image show snowshoes or snowsrackets
    * The image show a shoe but the image is altered (for instance the sole is split into the inner parts) or it is a zoom-in detail of the shoe (in this cases one or more of the edges of the picture terminates with a straight line as the product is cut inside the picture)
    * A person is wearing the footwear (i.e., the footwear is on a human subject).
    * The image is a diagram or a table.
    * A shoe is present against a non-neutral background (for instance, the background is not plain white, black, or gray)
    * The shoe displays missing parts or components from an adjacent shoe. (This error results from poor image segmentation and the splitting of multiple shoes.)

Here is the image:"""


class ShoeClassifierV1:
    """Pure-numpy 4-view shoe classifier (fallback model).

    Views: original, left_crop, right_crop, flipped (4 x 3072-dim)
    """

    def __init__(self, weights_path: str):
        """Initialize the classifier with weights from a file."""
        self.weights = dict(np.load(weights_path))
        self.classes = CLASSES

    def predict_probs(self, original, left_crop, right_crop, flipped):
        """Return probabilities for a single image."""
        views = [
            original[np.newaxis, :],
            left_crop[np.newaxis, :],
            right_crop[np.newaxis, :],
            flipped[np.newaxis, :],
        ]
        logits = self._forward(views)
        return self._softmax(logits[0])

    def predict_probs_batch(self, original, left_crop, right_crop, flipped):
        """Return probabilities for a batch."""
        logits = self._forward([original, left_crop, right_crop, flipped])
        return self._softmax(logits)

    def _forward(self, views):
        w = self.weights
        branch_outs = []
        for i, v in enumerate(views):
            out = v @ w[f"branch_{i}_W"].T + w[f"branch_{i}_b"]
            np.maximum(out, 0, out=out)
            branch_outs.append(out)
        concat = np.concatenate(branch_outs, axis=-1)
        h = concat @ w["head_W1"].T + w["head_b1"]
        np.maximum(h, 0, h)
        return h @ w["head_W2"].T + w["head_b2"]

    @staticmethod
    def _softmax(x):
        e = np.exp(x - np.max(x, axis=-1, keepdims=True))
        return e / e.sum(axis=-1, keepdims=True)


class ShoeClassifierV2:
    """Pure-numpy 5-view shoe side classifier with BatchNorm folded into weights.

    Views: original, left_crop, right_crop, flipped, text_image (5 x 3072-dim)
    Features: 12 dot_simple + 12 dot_desc + 3 cross-dot = 27 features
    """

    def __init__(self, weights_path: str):
        """Initialize the classifier with weights from a file."""
        self.weights = dict(np.load(weights_path))
        self.classes = CLASSES
        self.class_emb_matrix = self.weights.pop(
            "class_emb_matrix"
        )  # (12, 3072)
        self.class_desc_matrix = self.weights.pop(
            "class_desc_matrix"
        )  # (12, 3072)

    def compute_features(self, original, left_crop, right_crop):
        """Compute features from image embeddings."""
        dot_simple = original @ self.class_emb_matrix.T
        dot_desc = original @ self.class_desc_matrix.T
        dot_lr = np.sum(left_crop * right_crop, axis=-1, keepdims=True)
        dot_lf = np.sum(left_crop * original, axis=-1, keepdims=True)
        dot_rf = np.sum(right_crop * original, axis=-1, keepdims=True)
        return np.concatenate(
            [dot_simple, dot_desc, dot_lr, dot_lf, dot_rf], axis=-1
        )

    def predict_probs(
        self, original, left_crop, right_crop, flipped, text_image
    ):
        """Return probabilities for a single image."""
        features = self.compute_features(
            original[np.newaxis, :],
            left_crop[np.newaxis, :],
            right_crop[np.newaxis, :],
        )
        logits = self._forward(
            [
                original[np.newaxis, :],
                left_crop[np.newaxis, :],
                right_crop[np.newaxis, :],
                flipped[np.newaxis, :],
                text_image[np.newaxis, :],
            ],
            features,
        )
        return self._softmax(logits[0])

    def predict_probs_batch(
        self, original, left_crop, right_crop, flipped, text_image
    ):
        """Return probabilities for a batch."""
        features = self.compute_features(original, left_crop, right_crop)
        logits = self._forward(
            [original, left_crop, right_crop, flipped, text_image],
            features,
        )
        return self._softmax(logits)

    def _forward(self, views, features):
        w = self.weights
        branch_outs = []
        for i, v in enumerate(views):
            out = v @ w[f"branch_{i}_W"].T + w[f"branch_{i}_b"]
            np.maximum(out, 0, out=out)
            branch_outs.append(out)
        feat_out = features @ w["feat_proj_W"].T + w["feat_proj_b"]
        np.maximum(feat_out, 0, feat_out)
        concat = np.concatenate([*branch_outs, feat_out], axis=-1)
        h = concat @ w["head_W1"].T + w["head_b1"]
        np.maximum(h, 0, h)
        return h @ w["head_W2"].T + w["head_b2"]

    @staticmethod
    def _softmax(x):
        e = np.exp(x - np.max(x, axis=-1, keepdims=True))
        return e / e.sum(axis=-1, keepdims=True)


def _ensemble_predict(probs_v2, probs_v1):
    """Apply V1 override: if V2 says invalid but V1 disagrees with high confidence, use V1.

    Returns:
        (label, probs) where probs are from the chosen model.

    """
    pred_v2 = np.argmax(probs_v2)
    if pred_v2 == _INVALID_IDX:
        pred_v1 = np.argmax(probs_v1)
        if pred_v1 != _INVALID_IDX and probs_v1[pred_v1] >= _V1_OVERRIDE_CONF:
            logger.info(
                f"V1 override: V2=invalid, V1={CLASSES[pred_v1]} ({probs_v1[pred_v1]:.4f})"
            )
            return CLASSES[pred_v1], probs_v1
    return CLASSES[pred_v2], probs_v2


def _ensemble_predict_batch(probs_v2, probs_v1):
    """Batch version of V1 override.

    Returns:
        (labels, probs) lists.

    """
    preds_v2 = np.argmax(probs_v2, axis=-1)
    preds_v1 = np.argmax(probs_v1, axis=-1)
    confs_v1 = np.max(probs_v1, axis=-1)

    # Override mask: V2 says invalid, V1 disagrees with high confidence
    override = (
        (preds_v2 == _INVALID_IDX)
        & (preds_v1 != _INVALID_IDX)
        & (confs_v1 >= _V1_OVERRIDE_CONF)
    )

    final_preds = preds_v2.copy()
    final_preds[override] = preds_v1[override]
    final_probs = probs_v2.copy()
    final_probs[override] = probs_v1[override]

    n_overrides = override.sum()
    if n_overrides > 0:
        logger.info(
            f"V1 override applied to {n_overrides}/{len(preds_v2)} images"
        )

    labels = [CLASSES[i] for i in final_preds]
    return labels, final_probs


# Module-level singletons — loaded once at first import
_classifier_v2: ShoeClassifierV2 | None = None
_classifier_v1: ShoeClassifierV1 | None = None
_embedding_client: genai.Client | None = None


def _get_classifier_v2():
    global _classifier_v2
    if _classifier_v2 is None:
        logger.info(
            f"Loading V2 shoe classifier weights from {_WEIGHTS_V2_PATH}"
        )
        _classifier_v2 = ShoeClassifierV2(_WEIGHTS_V2_PATH)
    return _classifier_v2


def _get_classifier_v1():
    global _classifier_v1
    if _classifier_v1 is None:
        logger.info(
            f"Loading V1 shoe classifier weights from {_WEIGHTS_V1_PATH}"
        )
        _classifier_v1 = ShoeClassifierV1(_WEIGHTS_V1_PATH)
    return _classifier_v1


def _get_embedding_client():
    """Return a Gemini client configured for the embedding model region."""
    global _embedding_client
    if _embedding_client is None:
        project_id = os.getenv("PROJECT_ID", "my_project")
        location = os.getenv("US_REGION", "us-central1")
        logger.info(f"Creating embedding client for {location}")
        _embedding_client = genai.Client(
            vertexai=True, project=project_id, location=location
        )
    return _embedding_client


def _embed_image_views(
    image_bytes: bytes, client=None
) -> dict[str, np.ndarray]:
    """Embed an image and its 4 augmented views (left crop, right crop, flipped, text+image) in parallel."""
    client = _get_embedding_client()
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size

    views = {
        "original": img,
        "left_crop": img.crop((0, 0, w // 2, h)),
        "right_crop": img.crop((w // 2, 0, w, h)),
        "flipped": img.transpose(Image.FLIP_LEFT_RIGHT),
    }

    view_bytes = {}
    for name, view_img in views.items():
        buf = io.BytesIO()
        view_img.save(buf, format="PNG")
        view_bytes[name] = buf.getvalue()

    embeddings = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        # Image-only embeddings for 4 views
        futures = {
            executor.submit(embed_gemini, [img_b], client): name
            for name, img_b in view_bytes.items()
        }
        # Text+image embedding (system text + original image as PNG)
        futures[
            executor.submit(
                embed_gemini, [_SYSTEM_TEXT, view_bytes["original"]], client
            )
        ] = "text_image"

        for future in as_completed(futures):
            name = futures[future]
            embeddings[name] = future.result()

    return embeddings


def classify_shoe_local(image_bytes: bytes, client) -> str:
    """Classify a single shoe image using V2 model with V1 fallback.

    Args:
        image_bytes: Raw image bytes (PNG/JPEG).
        client: Gemini client for embedding calls.

    Returns:
        Predicted class label string.

    """
    clf_v2 = _get_classifier_v2()
    clf_v1 = _get_classifier_v1()
    embs = _embed_image_views(image_bytes, client)

    probs_v2 = clf_v2.predict_probs(
        embs["original"],
        embs["left_crop"],
        embs["right_crop"],
        embs["flipped"],
        embs["text_image"],
    )
    probs_v1 = clf_v1.predict_probs(
        embs["original"],
        embs["left_crop"],
        embs["right_crop"],
        embs["flipped"],
    )

    label, _ = _ensemble_predict(probs_v2, probs_v1)
    return label


def classify_shoe_local_batch(
    images_bytes_list: list[bytes], client, max_workers: int = 32
) -> list[str]:
    """Classify multiple shoe images in parallel using V2 model with V1 fallback.

    Args:
        images_bytes_list: List of raw image bytes.
        client: Gemini client for embedding calls.
        max_workers: Max parallel embedding workers.

    Returns:
        List of predicted class label strings.

    """
    clf_v2 = _get_classifier_v2()
    clf_v1 = _get_classifier_v1()

    all_embeddings = predict_parallel(
        images_bytes_list,
        lambda img_bytes: _embed_image_views(img_bytes, client),
        max_workers=max_workers,
        show_progress_bar=False,
    )

    original = np.stack([e["original"] for e in all_embeddings])
    left_crop = np.stack([e["left_crop"] for e in all_embeddings])
    right_crop = np.stack([e["right_crop"] for e in all_embeddings])
    flipped = np.stack([e["flipped"] for e in all_embeddings])
    text_image = np.stack([e["text_image"] for e in all_embeddings])

    probs_v2 = clf_v2.predict_probs_batch(
        original, left_crop, right_crop, flipped, text_image
    )
    probs_v1 = clf_v1.predict_probs_batch(
        original, left_crop, right_crop, flipped
    )

    labels, _ = _ensemble_predict_batch(probs_v2, probs_v1)
    return labels
