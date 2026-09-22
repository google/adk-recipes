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

"""KFP component: Preprocess — detect new/changed files and extract text.

This is a thin KFP wrapper around the shared pipeline.document_preprocessing
module. The @component decorator serializes this function into a self-contained
container step that Vertex AI Pipelines can schedule and execute independently.

All heavy logic (change detection via BQ Object Table, text extraction via
parallel HTTP fanout to the preprocess Cloud Run service, MERGE into BQ)
lives in the shared pipeline/ package.
"""

import os

from kfp.dsl import component

# The Docker image containing all dependencies (pipeline/, utils/, etc.).
# Built from data_ingestion_pipeline/Dockerfile and pushed to Artifact Registry.
# Set via PIPELINE_IMAGE env var in .env / Makefile.
PIPELINE_IMAGE = os.getenv("PIPELINE_IMAGE", "data-pipeline:latest")


# The @component decorator tells KFP to run this function inside the
# specified container image. KFP serializes the function body into an
# ephemeral Python script that the container executes — so imports must
# live inside the function (they resolve at runtime inside the container,
# not at compile time on the submitting machine).
@component(base_image=PIPELINE_IMAGE)
def preprocess(
    project_id: str,
    region: str,
    gcs_prefix: str,
    fq_object_table: str,
    fq_preprocessed_table: str,
) -> int:
    """Detect new/changed files in GCS and extract text to BQ.

    How it works:
    1. Queries the BQ Object Table (external table linked to GCS) and
       LEFT JOINs against the preprocessed table to find files that are
       new (not in preprocessed) or changed (different content_hash),
       and to spot content duplicates (cross-run + within-run).
    2. Dispatches non-duplicate URIs in parallel to the Cloud Run
       preprocess service (LibreOffice + Gemini) via authenticated HTTP.
    3. MERGEs extracted rows + duplicate stubs into the preprocessed
       table (upsert).

    Args:
        project_id: GCP project ID.
        region: GCP region (for BQ client).
        gcs_prefix: Prefix within the bucket (e.g. "documents/").
        fq_object_table: Fully-qualified BQ Object Table name
                         (e.g. "project.dataset.gcs_objects").
        fq_preprocessed_table: Fully-qualified BQ preprocessed table name
                               (e.g. "project.dataset.preprocessed").

    Returns:
        Number of files preprocessed (0 if nothing changed).
    """
    # Imports are inside the function because KFP serializes only the
    # function body. These modules are available inside the container
    # image (installed via uv pip install --system in the Dockerfile).
    from src.document_preprocessing import preprocess as preprocess_step
    from src.utils import bq_utils

    # Create a regional BQ client — region must match the dataset location
    bq_client = bq_utils.get_client(project_id, region)

    # Run the shared preprocessing logic. Returns the number of rows
    # written (extracted + duplicate stubs); rows are streamed to BQ
    # internally so the orchestrator never materializes them all.
    return preprocess_step.run(
        bq_client,
        project_id,
        region,
        gcs_prefix,
        fq_object_table,
        fq_preprocessed_table,
    )
