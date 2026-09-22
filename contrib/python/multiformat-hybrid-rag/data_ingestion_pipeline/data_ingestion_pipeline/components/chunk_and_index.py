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

"""KFP component: Chunk & Index — split text and ingest into VS2.

This is the most compute-intensive step of the pipeline. It takes the
extracted text from the preprocessed table, splits it into overlapping
chunks, generates contextual summaries per chunk (via Gemini), and writes
the results to both Vector Search 2.0 (for retrieval) and the BQ chunks
table (for tracking/debugging).

Uses BigFrames remote functions to deploy chunking + contextual enrichment
as a Cloud Function, so BigQuery executes it server-side. Scales to
terabytes without local memory pressure.

All logic lives in the shared pipeline/ package.
"""

import os

from kfp.dsl import component

# Container image with all dependencies installed system-wide.
# Must use uv pip install --system (not uv sync) because KFP's executor
# runs on the system Python, not inside a .venv.
PIPELINE_IMAGE = os.getenv("PIPELINE_IMAGE", "data-pipeline:latest")


@component(base_image=PIPELINE_IMAGE)
def chunk_and_index(
    project_id: str,
    region: str,
    vs_collection_id: str,
    vs_documents_collection_id: str,
    fq_preprocessed_table: str,
    fq_chunks_table: str,
    chunk_size: int,
    chunk_overlap: int,
    vs_batch_size: int = 250,
    rechunk_all: bool = False,
) -> int:
    """Split preprocessed text into chunks and index into VS2 + BQ.

    How it works:
    1. Queries the preprocessed table for files that need chunking:
       - New files (no matching entry in chunks table)
       - Re-extracted files (extracted_at > chunked_at)
       - All files if rechunk_all=True
    2. Splits each file's text using a markdown-aware splitter:
       - Respects headers (# / ## / ###) as natural boundaries
       - Falls back to paragraphs → lines → words for long sections
       - Maintains overlap between adjacent chunks for context continuity
    3. Generates a contextual summary per chunk using Gemini, describing
       what the chunk covers relative to the full document.
    4. Writes chunks to Vector Search 2.0 via batch create — VS2
       auto-generates embeddings using gemini-embedding-001 (3072d).
    5. Inserts chunk metadata into the BQ chunks table for tracking.

    Args:
        project_id: GCP project ID.
        region: GCP region (must match BQ dataset and VS2 collection).
        vs_collection_id: VS2 collection ID (e.g. "multiformat-hybrid-rag-collection").
        fq_preprocessed_table: Fully-qualified BQ preprocessed table
                               (e.g. "project.dataset.preprocessed").
        fq_chunks_table: Fully-qualified BQ chunks table
                         (e.g. "project.dataset.chunks").
        chunk_size: Maximum characters per chunk (default 1500 from config).
        chunk_overlap: Overlapping characters between adjacent chunks
                       (default 300 from config). Ensures context isn't
                       lost at chunk boundaries.
        vs_batch_size: Max data objects per VS2 batch create call.
                       API limit is 250.
        rechunk_all: If True, re-chunk all files regardless of timestamps.
                     Useful after changing chunk_size/chunk_overlap.

    Returns:
        Number of chunks created.
    """
    from src.chunking import chunk_and_index as chunk_step
    from src.utils import bq_utils

    # Build the full VS2 collection resource paths required by the API.
    # Format: projects/{project}/locations/{location}/collections/{collection}
    collection_path = f"projects/{project_id}/locations/{region}/collections/{vs_collection_id}"
    documents_collection_path = f"projects/{project_id}/locations/{region}/collections/{vs_documents_collection_id}"

    # Create a regional BQ client
    bq_client = bq_utils.get_client(project_id, region)

    # Run chunking + indexing. Returns number of chunks indexed.
    return chunk_step.run(
        bq_client,
        collection_path,
        documents_collection_path,
        fq_preprocessed_table,
        fq_chunks_table,
        chunk_size,
        chunk_overlap,
        vs_batch_size=vs_batch_size,
        rechunk_all=rechunk_all,
    )
