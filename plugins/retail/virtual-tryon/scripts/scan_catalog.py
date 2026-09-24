#!/usr/bin/env python3
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

"""Catalog Indexer utility.

Scans a local directory or Google Cloud Storage URI for product images
and uses Gemini 2.5 Flash to automatically classify and describe products,
outputting a catalog.json manifest.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from google.cloud import storage
from google.genai import types
from pydantic import BaseModel, Field

from scripts.config import config

os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class ProductAnalysis(BaseModel):
    category: str = Field(
        description="Must be one of: Clothing, Eyewear, Jewelry, Footwear, or Other"
    )
    description: str = Field(
        description="A 1-sentence clean description of the product item"
    )


def _get_client(project_id: str, location: str | None = None):
    from google import genai

    return genai.Client(
        vertexai=True,
        project=project_id,
        location=location or config.GCP_REGION,
    )


def scan_directory(
    directory_path: str,
    project_id: str,
    location: str | None = None,
    force_reindex: bool = False,
) -> list[dict]:
    """Scan directory (local path or gs:// URI) for images and classify them using Gemini."""
    is_gcs = directory_path.startswith("gs://")
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    catalog = []

    if is_gcs:
        logger.info(f"Scanning GCS Catalog path: {directory_path}...")

        # Parse GCS path
        gcs_path = directory_path[5:]
        if "/" in gcs_path:
            bucket_name, prefix = gcs_path.split("/", 1)
            # Normalize trailing slash for prefix
            if prefix and not prefix.endswith("/"):
                prefix += "/"
        else:
            bucket_name, prefix = gcs_path, ""

        gcs_client = storage.Client(project=project_id)
        bucket = gcs_client.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix=prefix))

        # Manifest lives in GCS root/prefix
        manifest_blob_name = (
            f"{prefix}catalog.json" if prefix else "catalog.json"
        )
        manifest_blob = bucket.blob(manifest_blob_name)

        existing_catalog = {}
        if manifest_blob.exists() and not force_reindex:
            try:
                manifest_text = manifest_blob.download_as_text()
                items = json.loads(manifest_text)
                existing_catalog = {item["image_path"]: item for item in items}
                logger.info(
                    f"Loaded {len(existing_catalog)} cached items from GCS manifest: {manifest_blob_name}"
                )
            except Exception as e:
                logger.warning(f"Failed to read existing GCS catalog.json: {e}")

        client = _get_client(project_id, location)

        for blob in blobs:
            # Skip folders, the manifest itself, or user reference files
            if blob.name == manifest_blob_name or blob.name.endswith("/"):
                continue
            suffix = Path(blob.name).suffix.lower()
            if suffix not in image_extensions:
                continue
            if "user" in blob.name.lower() or "person" in blob.name.lower():
                continue

            # Path served via proxy API
            rel_path = f"/api/media?path=gs://{bucket_name}/{blob.name}"

            if rel_path in existing_catalog and not force_reindex:
                logger.info(f"Using cached GCS metadata for {blob.name}")
                catalog.append(existing_catalog[rel_path])
                continue

            logger.info(
                f"Analyzing GCS product image with Gemini: {blob.name}..."
            )
            try:
                img_bytes = blob.download_as_bytes()

                response = client.models.generate_content(
                    model=config.GEMINI_TEXT_MODEL,
                    contents=[
                        types.Part.from_bytes(
                            data=img_bytes, mime_type="image/jpeg"
                        ),
                        "Analyze this product image and classify/describe the item.",
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ProductAnalysis,
                        temperature=0.1,
                    ),
                )

                result_json = json.loads(response.text)
                product_id = Path(blob.name).stem

                catalog.append(
                    {
                        "id": product_id,
                        "category": result_json.get("category", "Other"),
                        "description": result_json.get("description", ""),
                        "image_path": rel_path,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to analyze GCS image {blob.name}: {e}")
                catalog.append(
                    {
                        "id": Path(blob.name).stem,
                        "category": "Other",
                        "description": f"Product {Path(blob.name).stem}",
                        "image_path": rel_path,
                    }
                )

        # Write updated catalog back to GCS
        try:
            manifest_blob.upload_from_string(
                json.dumps(catalog, indent=2), content_type="application/json"
            )
            logger.info(
                f"Catalog manifest successfully saved to GCS: gs://{bucket_name}/{manifest_blob_name}"
            )
        except Exception as e:
            logger.error(f"Failed to write catalog.json back to GCS: {e}")

    else:
        # Scan local directory
        dir_path = Path(directory_path)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.error(f"Directory {directory_path} does not exist.")
            return []

        manifest_path = dir_path / "catalog.json"
        existing_catalog = {}

        if manifest_path.exists() and not force_reindex:
            try:
                with open(manifest_path) as f:
                    items = json.load(f)
                    existing_catalog = {
                        item["image_path"]: item for item in items
                    }
                logger.info(
                    f"Loaded {len(existing_catalog)} cached items from local {manifest_path}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to read existing local catalog.json: {e}"
                )

        image_files = []
        for p in dir_path.iterdir():
            if p.is_file() and p.suffix.lower() in image_extensions:
                if "user" in p.name.lower() or "person" in p.name.lower():
                    continue
                image_files.append(p)

        if not image_files:
            logger.warning(
                f"No image files found in local path {directory_path}"
            )
            return []

        client = _get_client(project_id, location)
        logger.info(f"Scanning {len(image_files)} local files...")

        for img_file in image_files:
            rel_path = str(img_file.relative_to(dir_path.parent))

            if rel_path in existing_catalog and not force_reindex:
                logger.info(f"Using cached metadata for local {rel_path}")
                catalog.append(existing_catalog[rel_path])
                continue

            logger.info(
                f"Analyzing local image with Gemini: {img_file.name}..."
            )
            try:
                with open(img_file, "rb") as f:
                    img_bytes = f.read()

                response = client.models.generate_content(
                    model=config.GEMINI_TEXT_MODEL,
                    contents=[
                        types.Part.from_bytes(
                            data=img_bytes, mime_type="image/jpeg"
                        ),
                        "Analyze this product image and classify/describe the item.",
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ProductAnalysis,
                        temperature=0.1,
                    ),
                )

                result_json = json.loads(response.text)
                product_id = img_file.stem

                catalog.append(
                    {
                        "id": product_id,
                        "category": result_json.get("category", "Other"),
                        "description": result_json.get("description", ""),
                        "image_path": rel_path,
                    }
                )
            except Exception as e:
                logger.error(
                    f"Failed to analyze local image {img_file.name}: {e}"
                )
                catalog.append(
                    {
                        "id": img_file.stem,
                        "category": "Other",
                        "description": f"Product {img_file.stem}",
                        "image_path": rel_path,
                    }
                )

        # Save to local catalog.json
        with open(manifest_path, "w") as f:
            json.dump(catalog, f, indent=2)
        logger.info(f"Catalog saved locally to {manifest_path}")

    return catalog


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scan folder and build product catalog using Gemini"
    )
    parser.add_argument(
        "directory",
        help="Local directory path or gs:// URI containing product images",
    )
    parser.add_argument(
        "--project-id", help="GCP project ID (overrides GOOGLE_CLOUD_PROJECT)"
    )
    parser.add_argument("--location", help="GCP region (overrides GCP_REGION)")
    parser.add_argument(
        "--force", action="store_true", help="Force reindexing of all images"
    )
    args = parser.parse_args()

    project_id = args.project_id or config.GOOGLE_CLOUD_PROJECT
    if not project_id:
        logger.error(
            "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in, "
            "or pass --project-id."
        )
        sys.exit(1)

    scan_directory(args.directory, project_id, args.location, args.force)
