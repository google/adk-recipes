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

"""KFP component: Cleanup — detect deleted files and cascade-remove.

Handles the reverse of ingestion: when a file is removed from the GCS
source bucket, this step detects it and cascade-deletes all derived data
(VS2 data objects, BQ chunks, BQ preprocessed row).

Deletion order is important for idempotency:
  1. Delete from VS2 first (search index)
  2. Delete from BQ chunks table
  3. Delete from BQ preprocessed table
If the step fails partway through and retries, the already-deleted
resources are simply no-ops, so no data is left dangling.

All logic lives in the shared pipeline/ package.
"""

import os

from kfp.dsl import component

# Container image — same as other components, shared across all steps.
PIPELINE_IMAGE = os.getenv("PIPELINE_IMAGE", "data-pipeline:latest")


@component(base_image=PIPELINE_IMAGE)
def cleanup(
    project_id: str,
    region: str,
    vs_collection_id: str,
    vs_documents_collection_id: str,
    fq_object_table: str,
    fq_preprocessed_table: str,
    fq_chunks_table: str,
) -> int:
    """Detect files deleted from GCS and cascade-remove from BQ + VS2.

    How it works:
    1. Queries the BQ Object Table (live GCS metadata) and compares
       against the preprocessed table to find "orphaned" files — rows
       in preprocessed that no longer have a corresponding GCS object.
    2. For each orphaned file_id:
       a. Deletes all data objects from VS2 (by file_id filter)
       b. Deletes all rows from the chunks table (WHERE file_id = ...)
       c. Deletes the row from the preprocessed table
    3. Returns the count of cleaned-up files.

    This step is conditional — it only runs when skip_cleanup=False
    in the pipeline DAG (controlled by dsl.Condition in pipeline.py).

    Args:
        project_id: GCP project ID.
        region: GCP region (must match BQ dataset and VS2 collection).
        vs_collection_id: VS2 chunks collection ID.
        vs_documents_collection_id: VS2 documents-by-file_id collection ID.
        fq_object_table: Fully-qualified BQ Object Table name. This is
                         an external table backed by GCS — it reflects
                         the live state of the bucket.
        fq_preprocessed_table: Fully-qualified BQ preprocessed table name.
        fq_chunks_table: Fully-qualified BQ chunks table name.

    Returns:
        Number of files cleaned up (0 if no deletions detected).
    """
    from src.removal import propagate_gcs_deletions
    from src.utils import bq_utils

    # Build the full VS2 collection resource paths
    collection_path = f"projects/{project_id}/locations/{region}/collections/{vs_collection_id}"
    documents_collection_path = f"projects/{project_id}/locations/{region}/collections/{vs_documents_collection_id}"

    bq_client = bq_utils.get_client(project_id, region)

    # Run cascade-delete. Returns list of file_ids that were removed.
    file_ids = propagate_gcs_deletions.run(
        bq_client,
        collection_path,
        documents_collection_path,
        fq_object_table,
        fq_preprocessed_table,
        fq_chunks_table,
    )
    return len(file_ids)
