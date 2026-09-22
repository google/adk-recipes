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

"""KFP pipeline definition for the GCS-based RAG ingestion pipeline.

Defines a three-stage Directed Acyclic Graph (DAG) that KFP compiles
into a JSON pipeline spec and submits to Vertex AI Pipelines:

    preprocess ──► chunk_and_index ──► cleanup (conditional)

Each stage is a separate container step running the Docker image from
Artifact Registry. Stages execute sequentially (enforced by .after()),
and each has automatic retries (up to 2) for transient failures.

This file only defines the DAG wiring and parameter passing — all
business logic lives in the shared pipeline/ package, which the
KFP components import at runtime inside their containers.

Run via `make data-ingestion-remote` (submits to Vertex AI Pipelines).
"""

from data_ingestion_pipeline.components.chunk_and_index import chunk_and_index
from data_ingestion_pipeline.components.cleanup import cleanup
from data_ingestion_pipeline.components.preprocess import preprocess
from kfp import dsl


@dsl.pipeline(
    description="RAG ingestion pipeline: preprocess GCS files, chunk, index into VS2, and clean up deletions"
)
def pipeline(
    project_id: str,
    region: str,
    gcs_prefix: str = "documents/",
    bq_dataset: str = "rag_pipeline",
    bq_object_table: str = "gcs_objects",
    bq_preprocessed_table: str = "preprocessed",
    bq_chunks_table: str = "chunks",
    vs_collection_id: str = "multiformat-hybrid-rag-collection",
    vs_documents_collection_id: str = "multiformat-hybrid-rag-documents",
    chunk_size: int = 800,
    chunk_overlap: int = 50,
    vs_batch_size: int = 250,
    rechunk_all: bool = False,
    skip_cleanup: bool = False,
) -> None:
    """RAG ingestion pipeline — processes GCS documents into VS2.

    All parameters default to .env values and are passed through
    by submit_pipeline.py. This single source of truth means the same
    config drives local dev, CI, and production runs.

    Args:
        project_id: GCP project ID (from .env GOOGLE_CLOUD_PROJECT).
        region: GCP region (from .env GOOGLE_CLOUD_LOCATION).
        gcs_prefix: Folder prefix within the bucket.
        bq_dataset: BigQuery dataset containing pipeline tables.
        bq_object_table: External table name linked to GCS bucket.
        bq_preprocessed_table: Table storing extracted text per file.
        bq_chunks_table: Table storing chunked text with metadata.
        vs_collection_id: Vector Search 2.0 collection for indexing.
        chunk_size: Max characters per chunk.
        chunk_overlap: Overlap between adjacent chunks.
        vs_batch_size: Objects per VS2 batch create call (max 250).
        rechunk_all: Force re-chunk of all files (e.g. after param change).
        skip_cleanup: Skip the deletion-detection step.
    """
    # Build fully-qualified BQ table names from project + dataset + table.
    # KFP evaluates these as string interpolations at pipeline compile time,
    # so downstream components receive the resolved table references.
    fq_object_table = f"{project_id}.{bq_dataset}.{bq_object_table}"
    fq_preprocessed_table = f"{project_id}.{bq_dataset}.{bq_preprocessed_table}"
    fq_chunks_table = f"{project_id}.{bq_dataset}.{bq_chunks_table}"

    # ── Step 1: Preprocess ──────────────────────────────────────────────
    # Detects new/changed files in GCS and extracts text into BQ.
    # Runs first with no dependencies. Retries up to 2 times on failure.
    preprocess_task = preprocess(
        project_id=project_id,
        region=region,
        gcs_prefix=gcs_prefix,
        fq_object_table=fq_object_table,
        fq_preprocessed_table=fq_preprocessed_table,
    ).set_retry(num_retries=2)

    # ── Step 2: Chunk & Index ───────────────────────────────────────────
    # Splits extracted text into chunks, generates contextual summaries,
    # and writes to VS2 + BQ. Must wait for preprocess to finish so that
    # newly extracted files are available for chunking.
    chunk_task = chunk_and_index(
        project_id=project_id,
        region=region,
        vs_collection_id=vs_collection_id,
        vs_documents_collection_id=vs_documents_collection_id,
        fq_preprocessed_table=fq_preprocessed_table,
        fq_chunks_table=fq_chunks_table,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        vs_batch_size=vs_batch_size,
        rechunk_all=rechunk_all,
    ).set_retry(num_retries=2)
    chunk_task.after(preprocess_task)

    # ── Step 3: Cleanup (conditional) ───────────────────────────────────
    # Detects files deleted from GCS and cascade-removes from VS2 + BQ.
    # Wrapped in dsl.Condition so it can be skipped via skip_cleanup=True.
    # Note: must use `== False` (not `is False`) because KFP compiles
    # conditions into CEL expressions that only support == comparisons.
    with dsl.Condition(skip_cleanup == False, name="run-cleanup"):  # noqa: E712
        cleanup_task = cleanup(
            project_id=project_id,
            region=region,
            vs_collection_id=vs_collection_id,
            vs_documents_collection_id=vs_documents_collection_id,
            fq_object_table=fq_object_table,
            fq_preprocessed_table=fq_preprocessed_table,
            fq_chunks_table=fq_chunks_table,
        ).set_retry(num_retries=2)
        cleanup_task.after(chunk_task)
