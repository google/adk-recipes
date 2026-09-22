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

"""Cleanup step: detect deleted files and cascade-remove from BQ and VS2.

This step reconciles the pipeline state against the GCS bucket to handle
file deletions. Since GCS doesn't emit real-time delete events that we
subscribe to, we detect deletions via a batch comparison:

    preprocessed LEFT JOIN Object Table WHERE obj.uri IS NULL

Any file that exists in our preprocessed table but no longer appears
in the Object Table (i.e., was deleted from GCS) gets cleaned up.

Cascade delete order:
    1. VS2 chunks data objects (semantic search index)
    2. VS2 documents data objects (per-file_id KV store)
    3. BQ chunks table (chunk-level metadata)
    4. BQ preprocessed table (file-level metadata)

This order is intentional — if the process crashes mid-way, a re-run
will pick up the remaining deletions because the preprocessed row
(checked in step 1's query) is deleted last.

Can be called from scripts or imported into a Vertex AI Pipeline component.
"""

from __future__ import annotations

import logging

from google.cloud import bigquery

from src.utils import bq_utils, vs_utils
from src.utils.config import config

logger = logging.getLogger(__name__)


def find_deleted_files(
    bq_client: bigquery.Client,
    fq_object_table: str,
    fq_preprocessed_table: str,
) -> list[dict]:
    """Find files that were deleted from GCS but still tracked in BQ.

    Compares the preprocessed table (our known state) against the
    Object Table (live GCS metadata). Files present in preprocessed
    but absent from the Object Table have been deleted from the bucket.

    Returns:
        List of dicts with keys: file_id, gcs_uri.
    """
    return bq_utils.get_deleted_files(
        bq_client, fq_object_table, fq_preprocessed_table
    )


def run(
    bq_client: bigquery.Client,
    collection_path: str,
    documents_collection_path: str,
    fq_object_table: str,
    fq_preprocessed_table: str,
    fq_chunks_table: str,
) -> list[str]:
    """Execute the full cleanup step.

    Main entry point, called by the Vertex AI Pipeline cleanup
    component. It performs:

    1. **Detect** — Query BQ to find files in the preprocessed table
       that no longer exist in the GCS Object Table.
    2. **Cascade delete** — For each deleted file, remove its data
       from all downstream stores in safe order:
       a. VS2 chunks data objects (query by file_id, batch delete)
       b. VS2 documents data objects (point delete by file_id)
       c. BQ chunks table rows (DELETE WHERE file_id IN ...)
       d. BQ preprocessed table rows (DELETE WHERE file_id IN ...)

    The cascade order ensures idempotency: if the process fails after
    deleting from VS2 but before deleting from BQ, the next run will
    still find the file in preprocessed (since it's deleted last) and
    retry the full cascade.

    Args:
        bq_client: Authenticated BigQuery client.
        collection_path: Full VS2 chunks collection resource path.
        documents_collection_path: Full VS2 documents-by-file_id collection path.
        fq_object_table: Fully-qualified BQ Object Table name.
        fq_preprocessed_table: Fully-qualified BQ preprocessed table name.
        fq_chunks_table: Fully-qualified BQ chunks table name.

    Returns:
        List of file_ids that were cleaned up (empty if nothing deleted).
    """
    deleted_files = find_deleted_files(
        bq_client, fq_object_table, fq_preprocessed_table
    )

    if not deleted_files:
        logger.info("No deleted files detected.")
        return []

    # Safety guard: every URI we're about to cascade-delete must live in
    # the bucket we're CURRENTLY configured to process. If GCS_BUCKET was
    # changed in config (typo, wrong env, etc.), the Object Table now
    # points at a different bucket and the join falsely flags the entire
    # old bucket's records as "deleted". Catch that before we cascade.
    expected_bucket = config.gcs_bucket
    expected_prefix = f"gs://{expected_bucket}/"
    mismatched = [
        f for f in deleted_files if not f["gcs_uri"].startswith(expected_prefix)
    ]
    if mismatched:
        raise RuntimeError(
            f"safety guard aborting cascade: {len(mismatched)} of "
            f"{len(deleted_files)} files marked for deletion don't match "
            f"GCS_BUCKET={expected_bucket}. Example: {mismatched[0]['gcs_uri']}. "
            "This usually means the bucket name in config changed since the "
            "preprocessed table was populated. Verify .env and the "
            "Object Table's URI prefix before re-running."
        )

    file_ids = [f["file_id"] for f in deleted_files]
    logger.info("Found %d deleted files:", len(deleted_files))
    for f in deleted_files:
        logger.info("  %s (file_id=%s)", f["gcs_uri"], f["file_id"])

    # 1. Delete from VS2 chunks collection first (search index)
    deleted_vs2 = vs_utils.delete_data_objects_by_file_ids(
        collection_path, file_ids
    )
    logger.info(
        "Deleted %d data objects from VS2 chunks collection.", deleted_vs2
    )

    # 2. Delete from VS2 documents collection (KV store keyed by file_id)
    deleted_docs = vs_utils.delete_documents_by_file_ids(
        documents_collection_path, file_ids
    )
    logger.info(
        "Deleted %d data objects from VS2 documents collection.", deleted_docs
    )

    # 3. Delete from chunks table (chunk-level metadata)
    deleted_chunks = bq_utils.delete_by_file_ids(
        bq_client, fq_chunks_table, file_ids
    )
    logger.info("Deleted %d rows from chunks table.", deleted_chunks)

    # 4. Delete from preprocessed table last (file-level metadata)
    deleted_prep = bq_utils.delete_by_file_ids(
        bq_client, fq_preprocessed_table, file_ids
    )
    logger.info("Deleted %d rows from preprocessed table.", deleted_prep)

    logger.info("Cleanup complete.")
    return file_ids
