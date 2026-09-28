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

"""Utilities for Google Cloud Storage operations using Transfer Manager."""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from google.cloud import storage
from google.cloud.storage import transfer_manager

logger = logging.getLogger(__name__)


def get_https(uri: str) -> str:
    """Convert a gs:// URI to an HTTPS URL for public access."""
    return f"https://storage.cloud.google.com/{uri.replace('gs://', '')}"


def get_storage_client(project_id: str | None = None) -> storage.Client:
    """Return a standard GCS client.

    Transfer Manager handles connection pooling internally, so custom adapters are rarely needed.
    """
    return storage.Client(project=project_id)


def upload_folder_to_gcs(
    bucket_name: str,
    source_folder_path: str,
    destination_prefix: str = "",
    project_id: str | None = None,
    include_extensions: list[str] | None = None,
    exclude_extensions: list[str] | None = None,
    max_workers: int = 50,
) -> list[str]:
    """Upload an entire folder to GCS using the High-Performance Transfer Manager.

    Args:
        bucket_name: Name of the GCS bucket
        source_folder_path: Local path to the folder to upload
        destination_prefix: Optional prefix (e.g. "my-folder/")
        project_id: Optional GCP project ID
        include_extensions: List of extensions to include (e.g., ['.jpg'])
        exclude_extensions: List of extensions to exclude
        max_workers: Number of parallel workers (Transfer Manager handles the pool)

    Returns:
        List of GCS URIs for all uploaded files.

    """
    source_path = Path(source_folder_path)

    if not source_path.exists():
        raise ValueError(f"Source folder does not exist: {source_folder_path}")
    if not source_path.is_dir():
        raise ValueError(f"Source path is not a folder: {source_folder_path}")

    # Ensure destination prefix is formatted correctly
    if destination_prefix and not destination_prefix.endswith("/"):
        destination_prefix += "/"

    # 1. Collect all files and prepare paths
    file_paths = []
    blob_names = []

    for root, _, files in os.walk(source_path):
        for filename in files:
            local_file_path = Path(root) / filename
            file_ext = local_file_path.suffix.lower()

            if include_extensions and file_ext not in include_extensions:
                continue
            if exclude_extensions and file_ext in exclude_extensions:
                continue

            # Calculate blob name
            relative_path = local_file_path.relative_to(source_path)
            blob_name = destination_prefix + str(relative_path).replace(
                "\\", "/"
            )

            file_paths.append(str(local_file_path))
            blob_names.append(blob_name)

    if not file_paths:
        logger.warning(f"No files found to upload in {source_folder_path}")
        return []

    logger.info(
        f"Preparing to upload {len(file_paths)} files using {max_workers} workers..."
    )

    # 2. Use Transfer Manager for parallel uploads with explicit blob names
    client = get_storage_client(project_id)
    bucket = client.bucket(bucket_name)

    # Create blob objects with explicit names
    blob_file_pairs = []
    for file_path, blob_name in zip(file_paths, blob_names, strict=False):
        blob = bucket.blob(blob_name)
        blob_file_pairs.append((file_path, blob))

    # Upload using transfer_manager with explicit blob objects
    results = transfer_manager.upload_many(
        blob_file_pairs, max_workers=max_workers, skip_if_exists=False
    )

    # 3. Process results
    uploaded_uris = []
    errors = []

    for (file_path, blob), result in zip(
        blob_file_pairs, results, strict=False
    ):
        if isinstance(result, Exception):
            errors.append(f"Failed to upload {file_path}: {result}")
        else:
            # Use the blob name we explicitly set
            uploaded_uris.append(f"gs://{bucket_name}/{blob.name}")

    if errors:
        for err in errors:
            logger.error(err)
        raise Exception(
            f"Failed to upload {len(errors)} files. See logs for details."
        )

    logger.info(
        f"✓ Successfully uploaded {len(uploaded_uris)} files to gs://{bucket_name}/{destination_prefix}"
    )
    return uploaded_uris


def upload_file_to_gcs(
    bucket_name: str,
    source_file_path: str,
    destination_blob_name: str,
    project_id: str | None = None,
    content_type: str | None = None,
) -> str:
    """Upload a single file to Google Cloud Storage."""
    source_path = Path(source_file_path)
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"Invalid source file: {source_file_path}")

    client = get_storage_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)

    if content_type:
        blob.content_type = content_type

    blob.upload_from_filename(str(source_path))

    gcs_uri = f"gs://{bucket_name}/{destination_blob_name}"
    logger.info(f"✓ Uploaded: {source_file_path} → {gcs_uri}")
    return gcs_uri


def upload_bytes_to_gcs(
    bucket_name: str,
    file_bytes: bytes,
    destination_blob_name: str,
    project_id: str | None = None,
    content_type: str | None = None,
) -> str:
    """Upload bytes directly to Google Cloud Storage."""
    client = get_storage_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)

    blob.upload_from_string(file_bytes, content_type=content_type)

    gcs_uri = f"gs://{bucket_name}/{destination_blob_name}"
    logger.info(f"✓ Uploaded {len(file_bytes)} bytes → {gcs_uri}")
    return gcs_uri


def download_file_from_gcs(
    bucket_name: str,
    source_blob_name: str,
    destination_file_path: str,
    project_id: str | None = None,
) -> str:
    """Download a file from Google Cloud Storage."""
    client = get_storage_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(source_blob_name)

    dest_path = Path(destination_file_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    blob.download_to_filename(str(dest_path))
    logger.info(
        f"✓ Downloaded: gs://{bucket_name}/{source_blob_name} → {destination_file_path}"
    )
    return str(dest_path)


def save_and_upload_to_gcs(
    result: dict[str, Any],
    product_id: str,
    bucket_name: str,
    gcs_destination_prefix: str = "shoe_spinning_outputs",
    project_id: str | None = None,
    pre_sampled_frames: list[bytes] | None = None,
    image_format: str = "png",
) -> list[str]:
    """Orchestrates saving video results locally and uploading to GCS.

    Args:
        result: Dictionary containing video and clip data
        product_id: Product identifier
        bucket_name: GCS bucket name
        gcs_destination_prefix: GCS prefix path
        project_id: Optional GCP project ID
        pre_sampled_frames: Pre-extracted and processed frames (REQUIRED - no extraction done here)
        image_format: Image format for saved frames (default: "png")

    Returns:
        List of uploaded file URIs

    """
    if pre_sampled_frames is None:
        raise ValueError(
            "pre_sampled_frames is required - frame extraction must be done before calling this function"
        )

    temp_dir = Path(tempfile.mkdtemp(prefix=f"{product_id}_"))
    logger.info(f"Created temporary directory: {temp_dir}")

    try:
        # --- Local File System Structure Setup ---
        video_dir = temp_dir / "video"
        video_frames_dir = video_dir / product_id
        clips_dir = temp_dir / "clips"

        for p in [video_dir, video_frames_dir, clips_dir]:
            p.mkdir(parents=True, exist_ok=True)

        logger.info("Processing final video...")

        # 1. Save Final Video
        final_video_path = video_dir / f"{product_id}.mp4"
        with open(final_video_path, "wb") as f:
            f.write(result["final_video"])

        if "original_video" in result and result["original_video"] is not None:
            original_video_path = video_dir / f"original_{product_id}.mp4"
            with open(original_video_path, "wb") as f:
                f.write(result["original_video"])

        # 2. Save pre-extracted frames (already sampled and processed)
        logger.info(f"Saving {len(pre_sampled_frames)} pre-extracted frames")
        for idx, frame_bytes in enumerate(pre_sampled_frames):
            with open(
                video_frames_dir / f"{product_id}-ld-{idx:04d}.{image_format}",
                "wb",
            ) as f:
                f.write(frame_bytes)

        # 3. Process Clips
        clips_metadata = []
        for clip in result["clips"]:
            clip_idx = clip["index"]
            folder_name = f"{product_id}_clip_{clip_idx}"
            clip_path = clips_dir / folder_name
            clip_path.mkdir(exist_ok=True)
            (clip_path / "frames").mkdir(exist_ok=True)

            # Save Video
            with open(clip_path / f"{folder_name}.mp4", "wb") as f:
                f.write(clip["video_bytes"])

            # Handle Reference Images (R2V) vs Start/End Frames (Standard)
            if "reference_images_bytes" in clip:
                ref_dir = clip_path / "reference_images"
                ref_dir.mkdir(exist_ok=True)
                for i, b in enumerate(clip["reference_images_bytes"]):
                    with open(ref_dir / f"reference_{i:02d}.png", "wb") as f:
                        f.write(b)

                if "reference_images_unstacked_bytes" in clip:
                    uns_dir = clip_path / "reference_images_unstacked"
                    uns_dir.mkdir(exist_ok=True)
                    for i, b in enumerate(
                        clip["reference_images_unstacked_bytes"]
                    ):
                        with open(
                            uns_dir / f"reference_unstacked_{i:02d}.png", "wb"
                        ) as f:
                            f.write(b)
            else:
                with open(clip_path / "start_frame.png", "wb") as f:
                    f.write(clip["start_frame_bytes"])
                with open(clip_path / "end_frame.png", "wb") as f:
                    f.write(clip["end_frame_bytes"])

            # Save clip frames (from frame_list in clip result)
            clip_frames = clip.get("frame_list", [])
            for i, b in enumerate(clip_frames):
                with open(
                    clip_path / "frames" / f"frame_{i:04d}.png", "wb"
                ) as f:
                    f.write(b)

            # Metadata building
            meta = {
                k: v
                for k, v in clip.items()
                if not k.endswith("bytes") and k != "frame_list"
            }
            meta["num_frames"] = len(clip_frames)
            clips_metadata.append(meta)

        # 4. Save Metadata
        with open(clips_dir / "clips_metadata.json", "w") as f:
            json.dump(
                {
                    "product_id": product_id,
                    "video_gen_status": result.get("video_gen_status"),
                    "num_clips": len(clips_metadata),
                    "final_video_frames": len(pre_sampled_frames),
                    "clips": clips_metadata,
                },
                f,
                indent=2,
            )

        # 5. Parallel Upload (Using the optimized function)
        gcs_prefix = (
            f"{gcs_destination_prefix}/{product_id}"
            if gcs_destination_prefix
            else product_id
        )

        uploaded_files = upload_folder_to_gcs(
            bucket_name=bucket_name,
            source_folder_path=str(temp_dir),
            destination_prefix=gcs_prefix,
            project_id=project_id,
            max_workers=50,  # Safe high number for Transfer Manager
        )

        # 6. Create & Upload Manifest
        manifest_data = _build_manifest(
            product_id,
            bucket_name,
            gcs_prefix,
            result,
            clips_metadata,
            pre_sampled_frames,
            temp_dir,
        )

        manifest_path = temp_dir / "gcs_manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)

        manifest_uri = upload_file_to_gcs(
            bucket_name,
            str(manifest_path),
            f"{gcs_prefix}/gcs_manifest.json",
            project_id,
            "application/json",
        )
        uploaded_files.append(manifest_uri)

        return uploaded_files

    finally:
        shutil.rmtree(temp_dir)
        logger.info(f"✓ Cleaned up {temp_dir}")


def _build_manifest(
    product_id, bucket, prefix, result, clips_meta, pre_sampled_frames, temp_dir
):
    """Construct the complicated manifest dictionary."""
    base_uri = f"gs://{bucket}/{prefix}/"
    manifest = {
        "product_id": product_id,
        "bucket": bucket,
        "base_uri": base_uri,
        "video": {
            "video_file": f"{base_uri}video/{product_id}.mp4",
            "frames": [
                f"{base_uri}video/frames/{product_id}_Id_{i:04d}.png"
                for i in range(len(pre_sampled_frames))
            ],
        },
        "clips": [],
        "metadata": f"{base_uri}clips/clips_metadata.json",
    }

    for clip, meta in zip(result["clips"], clips_meta, strict=False):
        clip_name = f"{product_id}_clip_{clip['index']}"
        clip_base = f"{base_uri}clips/{clip_name}"

        clip_entry = {
            "start_position": clip["start_position"],
            "end_position": clip["end_position"],
            "video_file": f"{clip_base}/{clip_name}.mp4",
            "frames": [],
        }

        # Frame classifications
        num_frames = meta["num_frames"]
        frame_map = {}
        if "sampled_frame_indices" in clip and "frame_classifications" in clip:
            frame_map = dict(
                zip(
                    clip["sampled_frame_indices"],
                    clip["frame_classifications"],
                    strict=False,
                )
            )

        for i in range(num_frames):
            clip_entry["frames"].append(
                [f"{clip_base}/frames/frame_{i:04d}.png", frame_map.get(i)]
            )

        # R2V vs Standard specific manifest entries
        if "reference_images_bytes" in clip:
            # Stacked
            ref_uris = []
            classes = clip.get("reference_images_classifications", [])
            for i in range(len(clip["reference_images_bytes"])):
                cls = classes[i] if i < len(classes) else None
                ref_uris.append(
                    [f"{clip_base}/reference_images/reference_{i:02d}.png", cls]
                )
            clip_entry["reference_images"] = ref_uris

            # Unstacked
            if "reference_images_unstacked_bytes" in clip:
                uns_uris = []
                uns_classes = clip.get(
                    "reference_images_unstacked_classifications", []
                )
                for i in range(len(clip["reference_images_unstacked_bytes"])):
                    cls = uns_classes[i] if i < len(uns_classes) else None
                    uns_uris.append(
                        [
                            f"{clip_base}/reference_images_unstacked/reference_unstacked_{i:02d}.png",
                            cls,
                        ]
                    )
                clip_entry["reference_images_unstacked"] = uns_uris
        else:
            clip_entry["start_frame"] = f"{clip_base}/start_frame.png"
            clip_entry["end_frame"] = f"{clip_base}/end_frame.png"

        manifest["clips"].append(clip_entry)

    return manifest
