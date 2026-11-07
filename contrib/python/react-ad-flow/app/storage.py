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

"""Unified storage abstraction for local and GCS.

This module provides a unified interface for reading/writing assets (images, videos)
that works in both local development and Cloud Run deployment modes.

API Methods Used (verified via Context7, December 2025):
- blob.download_as_bytes() - download blob content as bytes
- blob.upload_from_file(file_obj, content_type=...) - upload from file-like object
- blob.exists() - check if blob exists
- bucket.list_blobs(prefix=...) - list blobs with prefix

Mode Detection:
- If GCS_BUCKET env var is set -> GCS mode (Cloud Run)
- If GCS_BUCKET is not set -> Local mode (development)
"""

import io
import os

# Lazy GCS initialization to avoid import errors when running locally
_gcs_client = None
_bucket = None


def _get_bucket():
    """Get GCS bucket (lazy initialization).

    Only initializes the GCS client when first needed, avoiding import
    errors when google-cloud-storage is not installed locally.
    """
    global _gcs_client, _bucket
    from .config import GCS_BUCKET

    if _bucket is None and GCS_BUCKET:
        from google.cloud import storage

        _gcs_client = storage.Client()
        _bucket = _gcs_client.bucket(GCS_BUCKET)
    return _bucket


def get_storage_mode() -> str:
    """Return 'gcs' or 'local' based on configuration.

    Returns:
        'gcs' if GCS_BUCKET is configured, 'local' otherwise.
    """
    from .config import GCS_BUCKET

    return "gcs" if GCS_BUCKET else "local"


# =============================================================================
# Image Storage Functions
# =============================================================================


def list_seed_images() -> list[str]:
    """List available seed image filenames.

    Returns:
        List of image filenames (without path prefix).
    """
    from .config import SELECTED_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blobs = bucket.list_blobs(prefix="seed-images/")
        return [
            b.name.replace("seed-images/", "")
            for b in blobs
            if not b.name.endswith("/")
        ]
    else:
        if not os.path.exists(SELECTED_DIR):
            return []
        return [
            f
            for f in os.listdir(SELECTED_DIR)
            if f.endswith((".jpg", ".jpeg", ".png"))
        ]


def image_exists(filename: str) -> bool:
    """Check if a seed image exists.

    Args:
        filename: Image filename (without path).

    Returns:
        True if the image exists, False otherwise.
    """
    from .config import SELECTED_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"seed-images/{filename}")
        return blob.exists()
    else:
        return os.path.exists(os.path.join(SELECTED_DIR, filename))


def read_image(filename: str) -> bytes:
    """Read image bytes from storage.

    Uses blob.download_as_bytes() - the correct method per GCS docs.
    Note: download_as_string() is deprecated.

    Args:
        filename: Image filename (without path).

    Returns:
        Image data as bytes.
    """
    from .config import SELECTED_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"seed-images/{filename}")
        return blob.download_as_bytes()
    else:
        with open(os.path.join(SELECTED_DIR, filename), "rb") as f:
            return f.read()


def save_image(filename: str, data: bytes) -> str:
    """Save image to storage, return the path/URL.

    Uses blob.upload_from_file() with BytesIO wrapper per GCS docs.

    Args:
        filename: Image filename to save as.
        data: Image data as bytes.

    Returns:
        Full path (local) or gs:// URL (GCS) of saved image.
    """
    from .config import GCS_BUCKET, SELECTED_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"seed-images/{filename}")
        # Use upload_from_file with BytesIO (documented API)
        blob.upload_from_file(
            io.BytesIO(data), content_type="image/png", rewind=True
        )
        return f"gs://{GCS_BUCKET}/seed-images/{filename}"
    else:
        os.makedirs(SELECTED_DIR, exist_ok=True)
        path = os.path.join(SELECTED_DIR, filename)
        with open(path, "wb") as f:
            f.write(data)
        return path


def get_image_path(filename: str) -> str:
    """Get the full path/URL for a seed image.

    Args:
        filename: Image filename (without path).

    Returns:
        Full path (local) or gs:// URL (GCS).
    """
    from .config import GCS_BUCKET, SELECTED_DIR

    if get_storage_mode() == "gcs":
        return f"gs://{GCS_BUCKET}/seed-images/{filename}"
    else:
        return os.path.join(SELECTED_DIR, filename)


# =============================================================================
# Product Image Storage Functions
# =============================================================================


def _product_image_content_type(filename: str) -> str:
    return (
        "image/jpeg"
        if filename.lower().endswith((".jpg", ".jpeg"))
        else "image/png"
    )


def product_image_exists(filename: str) -> bool:
    """Check if a product image exists (GCS product-images/ or local dir)."""
    from .config import PRODUCT_IMAGES_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"product-images/{filename}")
        return blob.exists()
    return os.path.exists(os.path.join(PRODUCT_IMAGES_DIR, filename))


def read_product_image(filename: str) -> bytes:
    """Read product image bytes from storage."""
    from .config import PRODUCT_IMAGES_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"product-images/{filename}")
        return blob.download_as_bytes()
    with open(os.path.join(PRODUCT_IMAGES_DIR, filename), "rb") as f:
        return f.read()


def save_product_image(filename: str, data: bytes) -> str:
    """Save a product image, return the local path or gs:// URL."""
    from .config import GCS_BUCKET, PRODUCT_IMAGES_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"product-images/{filename}")
        blob.upload_from_file(
            io.BytesIO(data),
            content_type=_product_image_content_type(filename),
            rewind=True,
        )
        return f"gs://{GCS_BUCKET}/product-images/{filename}"
    os.makedirs(PRODUCT_IMAGES_DIR, exist_ok=True)
    path = os.path.join(PRODUCT_IMAGES_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


def get_product_image_path(filename: str) -> str:
    """Get the full local path or gs:// URL for a product image."""
    from .config import GCS_BUCKET, PRODUCT_IMAGES_DIR

    if get_storage_mode() == "gcs":
        return f"gs://{GCS_BUCKET}/product-images/{filename}"
    return os.path.join(PRODUCT_IMAGES_DIR, filename)


# =============================================================================
# Video Storage Functions
# =============================================================================


def save_video(filename: str, data: bytes) -> str:
    """Save video to storage, return the path/URL.

    Uses blob.upload_from_file() with BytesIO wrapper per GCS docs.

    Args:
        filename: Video filename to save as.
        data: Video data as bytes.

    Returns:
        Full path (local) or gs:// URL (GCS) of saved video.
    """
    from .config import GCS_BUCKET, GENERATED_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        blob = bucket.blob(f"generated/{filename}")
        # Use upload_from_file with BytesIO (documented API)
        blob.upload_from_file(
            io.BytesIO(data), content_type="video/mp4", rewind=True
        )
        return f"gs://{GCS_BUCKET}/generated/{filename}"
    else:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        path = os.path.join(GENERATED_DIR, filename)
        with open(path, "wb") as f:
            f.write(data)
        return path


def read_video(path_or_filename: str) -> bytes:
    """Read video bytes from storage.

    Uses blob.download_as_bytes() - the correct method per GCS docs.

    Args:
        path_or_filename: Can be a filename, relative path (generated/...),
                         or gs:// URL.

    Returns:
        Video data as bytes.
    """
    from .config import GCS_BUCKET, GENERATED_DIR

    if get_storage_mode() == "gcs":
        bucket = _get_bucket()
        # Handle both gs:// URLs and bare filenames
        if path_or_filename.startswith("gs://"):
            blob_path = path_or_filename.replace(f"gs://{GCS_BUCKET}/", "")
        elif path_or_filename.startswith("generated/"):
            blob_path = path_or_filename
        else:
            blob_path = f"generated/{path_or_filename}"
        blob = bucket.blob(blob_path)
        return blob.download_as_bytes()
    else:
        if os.path.isabs(path_or_filename):
            path = path_or_filename
        elif path_or_filename.startswith("generated/"):
            path = os.path.join(
                os.path.dirname(GENERATED_DIR), path_or_filename
            )
        else:
            path = os.path.join(GENERATED_DIR, path_or_filename)
        with open(path, "rb") as f:
            return f.read()


def get_video_path(filename: str) -> str:
    """Get the full path/URL for a video.

    Args:
        filename: Video filename (without path).

    Returns:
        Relative path (local) or gs:// URL (GCS).
    """
    from .config import GCS_BUCKET

    if get_storage_mode() == "gcs":
        return f"gs://{GCS_BUCKET}/generated/{filename}"
    else:
        return f"generated/{filename}"


def video_exists(path_or_filename: str) -> bool:
    """Check if a generated video exists.

    Args:
        path_or_filename: Can be a filename, relative path (generated/...),
                         or gs:// URL.

    Returns:
        True if the video exists, False otherwise.
        Returns False on any error (permission denied, network issues, etc.)
    """
    from .config import GCS_BUCKET, GENERATED_DIR

    if get_storage_mode() == "gcs":
        try:
            bucket = _get_bucket()
            if bucket is None:
                return False
            # Handle gs:// URLs, relative paths, and bare filenames
            if path_or_filename.startswith("gs://"):
                blob_path = path_or_filename.replace(f"gs://{GCS_BUCKET}/", "")
            elif path_or_filename.startswith("generated/"):
                blob_path = path_or_filename
            else:
                blob_path = f"generated/{path_or_filename}"
            blob = bucket.blob(blob_path)
            return blob.exists()
        except Exception:
            # Return False on any GCS error (permissions, network, etc.)
            # This prevents cascading failures when GCS is inaccessible
            return False
    else:
        # Local mode: handle various path formats
        if path_or_filename.startswith("generated/"):
            path = os.path.join(
                os.path.dirname(GENERATED_DIR), path_or_filename
            )
        elif os.path.isabs(path_or_filename):
            path = path_or_filename
        else:
            path = os.path.join(GENERATED_DIR, path_or_filename)
        return os.path.exists(path)


# =============================================================================
# Public URL Functions (for public GCS bucket access)
# =============================================================================


def get_public_url(blob_path: str) -> str | None:
    """Get a public URL for a GCS blob.

    Assumes the bucket has public read access configured.
    Returns None if GCS is not configured.

    Args:
        blob_path: Path in bucket (e.g., 'generated/video.mp4')

    Returns:
        Public URL string or None if not in GCS mode
    """
    from .config import GCS_BUCKET

    if not GCS_BUCKET:
        return None
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_path}"


def get_video_public_url(
    filename: str, check_exists: bool = False
) -> str | None:
    """Get public URL for a generated video.

    Args:
        filename: Video filename (without path prefix)
        check_exists: If True, verify file exists before returning URL

    Returns:
        Public URL string or None if not in GCS mode or file doesn't exist
    """
    if check_exists and not video_exists(filename):
        return None
    return get_public_url(f"generated/{filename}")


def get_thumbnail_public_url(
    filename: str, check_exists: bool = True
) -> str | None:
    """Public URL for a video thumbnail (generated/ prefix), or None.

    None when not in GCS mode, or (by default) when the file doesn't exist —
    tool responses must never carry dead storage URLs (ws09 bar).
    """
    if check_exists and not video_exists(filename):
        return None
    return get_public_url(f"generated/{filename}")


def get_product_image_public_url(
    filename: str, check_exists: bool = True
) -> str | None:
    """Public URL for a product image, or None.

    None when not in GCS mode, or (by default) when the blob doesn't exist —
    tool responses must never carry dead storage URLs (ws09 bar).
    """
    if get_storage_mode() != "gcs":
        return None
    if check_exists and not product_image_exists(filename):
        return None
    return get_public_url(f"product-images/{filename}")
