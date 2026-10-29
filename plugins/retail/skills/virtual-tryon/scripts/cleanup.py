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

r"""Clean up GCP resources created for virtual try-on.

Deletes GCS buckets created for user photo uploads, generated try-on output,
and optional try-on catalog storage.

Usage:
    # Dry run (show what would be deleted, don't delete)
    python scripts/cleanup.py --dry-run

    # Delete everything
    python scripts/cleanup.py --confirm

    # With explicit project ID
    python scripts/cleanup.py --project-id my-project --confirm
"""

import argparse
import logging
import os
import pathlib
import sys

# Allow imports from the script's own directory before pip install -e is run.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from google.api_core import exceptions
from google.cloud import storage

from scripts.config import config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def delete_bucket(project_id: str, bucket_name: str, dry_run: bool) -> bool:
    """Delete a GCS bucket and all of its contents.

    Args:
        project_id: GCP project ID.
        bucket_name: Name of the GCS bucket.
        dry_run: If True, log what would be deleted without deleting.

    Returns:
        True on successful delete or when the bucket is absent.
        False on unexpected failure.
    """
    client = storage.Client(project=project_id)
    try:
        bucket = client.get_bucket(bucket_name)
    except exceptions.NotFound:
        logger.info("Bucket gs://%s does not exist, skipping.", bucket_name)
        return True
    except Exception as e:
        logger.error("Failed to check bucket gs://%s: %s", bucket_name, e)
        return False

    if dry_run:
        logger.info(
            "[DRY RUN] Would delete GCS bucket: gs://%s (and all blobs)",
            bucket_name,
        )
        return True

    try:
        logger.info("Deleting all blobs in gs://%s...", bucket_name)
        blobs = list(bucket.list_blobs())
        for blob in blobs:
            blob.delete()
        bucket.delete(force=True)
        logger.info("Deleted GCS bucket gs://%s.", bucket_name)
        return True
    except Exception as e:
        logger.error("Failed to delete bucket gs://%s: %s", bucket_name, e)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up GCP resources created for virtual try-on."
    )
    parser.add_argument(
        "--project-id",
        help="GCP Project ID (defaults to GOOGLE_CLOUD_PROJECT env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without making changes",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm deletion of resources (required unless --dry-run is set)",
    )
    args = parser.parse_args()

    project_id = (
        args.project_id
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or config.GOOGLE_CLOUD_PROJECT
    )
    if not project_id:
        logger.error(
            "Error: Project ID required. Set GOOGLE_CLOUD_PROJECT or pass --project-id."
        )
        sys.exit(1)

    if not args.dry_run and not args.confirm:
        logger.error("Error: Must specify either --dry-run or --confirm.")
        sys.exit(1)

    output_bucket = config.TRYON_OUTPUT_BUCKET or f"{project_id}-tryon-output"
    upload_bucket = config.TRYON_UPLOAD_BUCKET or f"{project_id}-tryon-uploads"
    catalog_bucket = f"{project_id}-tryon-catalog"

    buckets_to_clean = [output_bucket, upload_bucket, catalog_bucket]
    logger.info(
        "Cleaning up virtual try-on resources in project: %s", project_id
    )

    all_ok = True
    for bucket in buckets_to_clean:
        if bucket:
            ok = delete_bucket(project_id, bucket, args.dry_run)
            if not ok:
                all_ok = False

    if all_ok:
        logger.info("Cleanup completed successfully.")
    else:
        logger.error("Cleanup encountered errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
