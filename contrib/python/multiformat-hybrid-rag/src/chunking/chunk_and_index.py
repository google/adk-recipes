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

"""Chunk-and-index step: split preprocessed text into chunks and ingest into VS2.

This is the second stage of the two-stage ingestion pipeline. It takes the
already-extracted text from the BQ preprocessed table and dispatches one
HTTP request per file to the `chunk-index` Cloud Run service. The service
splits the text, generates contextual chunks via Gemini, and indexes them
into both Vector Search 2.0 collections (chunks + documents). On successful
response, the orchestrator inserts the returned chunk rows into the BQ
chunks table with `indexed_at` set.

Why no BigFrames / BQ remote function:
    BigQuery dispatches REMOTE FUNCTION calls based on slot allocation
    (data-size proportional), capping us at ~20 instances even on 1k rows.
    Direct HTTP fanout from the orchestrator gives deterministic N-way
    parallelism and lets Cloud Run's autoscaler do its job.

Chunk identity:
    chunk_id = "{file_id}__{chunk_index}"
    Deterministic given the same file content and chunk parameters. When
    a file is re-chunked, the service first deletes the old VS2 entries
    for this file_id; the orchestrator deletes old BQ chunks before
    inserting the new ones.

VS2 auto-embeddings:
    Data objects are created with empty vectors — VS 2.0 auto-generates
    embeddings using the configured model and the collection's text_template.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from google.cloud import bigquery, run_v2

from src.utils import CHUNK_ID_SEPARATOR, bq_utils, vs_utils
from src.utils.auth import get_id_token
from src.utils.config import config as pipeline_config
from src.utils.http_utils import post_with_retry

logger = logging.getLogger(__name__)

CHUNK_INDEX_SERVICE_NAME = os.getenv(
    "CHUNK_INDEX_SERVICE_NAME", "multiformat-hybrid-rag-chunk-index"
)
DEFAULT_MAX_WORKERS = int(os.getenv("CHUNK_INDEX_MAX_WORKERS", "200"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("CHUNK_INDEX_HTTP_TIMEOUT", "900"))
HTTP_RETRY_ATTEMPTS = int(os.getenv("CHUNK_INDEX_HTTP_RETRIES", "4"))
# How many chunk rows to accumulate before flushing to the BQ staging
# table. With ~16 chunks/file x 100 files per batch ~ 1600 rows per
# flush, well under the load-job sweet spot.
FLUSH_BATCH_SIZE = int(os.getenv("CHUNK_INDEX_FLUSH_BATCH_SIZE", "1500"))
# How many successful files to accumulate before issuing one DELETE for
# their old chunks. Chunks for a file are only deleted AFTER the service
# confirms the new ones are ready (so an outage doesn't leave the file
# with empty results), and we batch the deletes so a 1k-file run does
# ~10 small DELETEs instead of 1k.
DELETE_BATCH_SIZE = int(os.getenv("CHUNK_INDEX_DELETE_BATCH_SIZE", "100"))

_CHUNK_SCHEMA = [
    bigquery.SchemaField("chunk_id", "STRING"),
    bigquery.SchemaField("file_id", "STRING"),
    bigquery.SchemaField("gcs_uri", "STRING"),
    bigquery.SchemaField("chunk_index", "INT64"),
    bigquery.SchemaField("chunk_text", "STRING"),
    bigquery.SchemaField("context", "STRING"),
]


def _staging_table_name(fq_chunks_table: str, run_id: str) -> str:
    """Per-run staging name: prevents two concurrent pipeline runs from
    clobbering each other. If a run crashes the table is left behind
    under its run_id — easier to debug than a singleton table."""
    return f"{fq_chunks_table.rsplit('.', 1)[0]}._staging_chunk_rows_{run_id}"


def _prepare_chunks_staging(
    bq_client: bigquery.Client, fq_chunks_table: str, run_id: str
) -> None:
    """Create/truncate the chunks staging table for this run."""
    bq_client.load_table_from_json(
        [],
        _staging_table_name(fq_chunks_table, run_id),
        job_config=bigquery.LoadJobConfig(
            schema=_CHUNK_SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        ),
    ).result()


def _append_chunks_batch(
    bq_client: bigquery.Client,
    fq_chunks_table: str,
    rows: list[dict],
    run_id: str,
) -> None:
    """Append a batch of chunk rows to the staging table."""
    if not rows:
        return
    bq_client.load_table_from_json(
        rows,
        _staging_table_name(fq_chunks_table, run_id),
        job_config=bigquery.LoadJobConfig(
            schema=_CHUNK_SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        ),
    ).result()


def _flush_chunks_staging(
    bq_client: bigquery.Client, fq_chunks_table: str, run_id: str
) -> None:
    """INSERT staging → target, stamp timestamps, then drop staging."""
    staging_table = _staging_table_name(fq_chunks_table, run_id)
    # Plain INSERT (not MERGE): the orchestrator already deleted the old
    # rows for these file_ids before staging, so there is nothing to
    # update. Timestamps are stamped server-side at INSERT time.
    insert_sql = f"""
    INSERT INTO `{fq_chunks_table}`
        (chunk_id, file_id, gcs_uri, chunk_index, chunk_text, context, chunked_at, indexed_at)
    SELECT
        chunk_id,
        file_id,
        gcs_uri,
        chunk_index,
        chunk_text,
        context,
        CURRENT_TIMESTAMP() AS chunked_at,
        CURRENT_TIMESTAMP() AS indexed_at
    FROM `{staging_table}`
    """
    bq_client.query(insert_sql).result()
    bq_client.delete_table(staging_table, not_found_ok=True)


def _build_rechunk_query(
    fq_preprocessed_table: str,
    fq_chunks_table: str,
    rechunk_all: bool = False,
) -> str:
    """Build the SQL query to find files needing chunking.

    Two modes:
    - Incremental (default): files re-extracted since last successful index,
      or files that have no chunks yet.
    - Full (rechunk_all=True): every relevant preprocessed file.

    Compares extracted_at against MAX(indexed_at), NOT chunked_at:
    indexed_at is only stamped after the chunk-index service confirms VS2
    success, so files with VS2 failures are naturally retried next run.
    """
    if rechunk_all:
        # Rebuild every chunk regardless of indexing state. Used after
        # changing chunk_size / chunk_overlap / contextual prompt.
        # No "find candidates first" trick here — we genuinely need every
        # relevant row's content.
        return f"""
        -- relevant_files: every preprocessed file the classifier didn't reject.
        -- `relevant IS NOT FALSE` accepts both TRUE and NULL; FALSE rows are
        -- stubs (dup, scanner probe, classifier-irrelevant).
        WITH relevant_files AS (
            SELECT file_id, gcs_uri, content
            FROM `{fq_preprocessed_table}`
            WHERE relevant IS NOT FALSE
        )
        SELECT * FROM relevant_files
        """

    # Two-phase plan to avoid scanning the heavy `content` column for files
    # we won't actually re-chunk:
    #   1. `candidates` — pure-metadata scan to find which file_ids need
    #      re-chunking. No content read.
    #   2. Final SELECT — JOIN candidates back to preprocessed by file_id
    #      to fetch content for ONLY those rows. With CLUSTER BY file_id
    #      on preprocessed, this prunes to the matching clusters.
    return f"""
    WITH
      -- Step 1 — last_indexed_per_file: per-file timestamp of the most
      -- recent successful VS2 indexing. `indexed_at IS NOT NULL` excludes
      -- failed indexes (the orchestrator only stamps indexed_at after the
      -- chunk-index service confirms VS2 success), so a file with a
      -- half-indexed previous run is naturally retried.
      last_indexed_per_file AS (
        SELECT file_id, MAX(indexed_at) AS last_indexed
        FROM `{fq_chunks_table}`
        WHERE indexed_at IS NOT NULL
        GROUP BY file_id
      ),

      -- Step 2 — candidates: file_ids that need (re-)chunking, computed
      -- WITHOUT touching the heavy `content` column. A file is a candidate
      -- if it was never indexed (l.file_id IS NULL) or was re-extracted
      -- after the last successful index (extracted_at > last_indexed).
      candidates AS (
        SELECT p.file_id
        FROM `{fq_preprocessed_table}` AS p
        LEFT JOIN last_indexed_per_file AS l USING (file_id)
        WHERE p.relevant IS NOT FALSE
          AND (l.file_id IS NULL OR p.extracted_at > l.last_indexed)
      )

    -- Final SELECT: fetch content for ONLY the candidate rows.
    -- preprocessed is CLUSTER BY file_id, so this JOIN scans only the
    -- clusters that contain matching file_ids — not the whole content
    -- column.
    SELECT p.file_id, p.gcs_uri, p.content
    FROM candidates AS c
    JOIN `{fq_preprocessed_table}` AS p USING (file_id)
    """


def _resolve_service_url(project_id: str, region: str) -> str:
    """Look up the chunk-index Cloud Run service URL via the Run admin API.

    Env var `CHUNK_INDEX_SERVICE_URL` short-circuits the lookup (useful
    for local testing).
    """
    override = os.getenv("CHUNK_INDEX_SERVICE_URL")
    if override:
        return override
    client = run_v2.ServicesClient()
    name = f"projects/{project_id}/locations/{region}/services/{CHUNK_INDEX_SERVICE_NAME}"
    return client.get_service(name=name).uri


def _process_one(
    service_url: str,
    file_row: dict,
    chunk_size: int,
    chunk_overlap: int,
) -> dict:
    """Send one file to the chunk-index service, return parsed reply.

    Reply shape: {"file_id": "...", "indexed": true, "chunks": [...]}
    Each chunk: {"chunk_index": int, "chunk_text": str, "context": str}

    Calls `get_id_token` per request — the helper caches the token per
    audience and refreshes ~10 min before the 60-min expiry, so long
    runs don't hit silent 401 storms when the original token expires.
    """
    return post_with_retry(
        service_url,
        headers={
            "Authorization": f"Bearer {get_id_token(service_url)}",
            "Content-Type": "application/json",
        },
        body={
            "file_id": file_row["file_id"],
            "gcs_uri": file_row["gcs_uri"],
            "content": file_row["content"] or "",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
        attempts=HTTP_RETRY_ATTEMPTS,
    )


def _build_chunk_rows(file_row: dict, reply: dict) -> list[dict]:
    """Shape per-chunk BQ rows from a service reply."""
    file_id = file_row["file_id"]
    gcs_uri = file_row["gcs_uri"]
    rows: list[dict] = []
    for chunk in reply.get("chunks", []):
        rows.append(
            {
                "chunk_id": (
                    f"{file_id}{CHUNK_ID_SEPARATOR}{chunk['chunk_index']}"
                ),
                "file_id": file_id,
                "gcs_uri": gcs_uri,
                "chunk_index": chunk["chunk_index"],
                "chunk_text": chunk["chunk_text"],
                "context": chunk.get("context", ""),
            }
        )
    return rows


def chunk_and_index_remote(
    bq_client: bigquery.Client,
    project_id: str,
    region: str,
    fq_preprocessed_table: str,
    fq_chunks_table: str,
    chunk_size: int,
    chunk_overlap: int,
    rechunk_all: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    flush_batch_size: int = FLUSH_BATCH_SIZE,
) -> tuple[list[str], int]:
    """Detect files needing (re-)chunking and dispatch to the Cloud Run service.

    Streams chunk rows to a BQ staging table in batches so the orchestrator
    never holds more than ~`flush_batch_size` chunks in memory. After all
    work completes a single INSERT moves staging → target.

    Returns (successful_file_ids, total_chunks_indexed). Files for which
    the service returned an error are logged and excluded from the BQ
    insert; they will be retried on the next run because their indexed_at
    stays NULL.
    """
    # Per-run staging suffix: prevents two concurrent pipeline runs from
    # clobbering each other's staging table. Combines wallclock seconds
    # with a random suffix.
    run_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    logger.info("chunk_and_index run_id=%s", run_id)

    query = _build_rechunk_query(
        fq_preprocessed_table, fq_chunks_table, rechunk_all
    )
    rows_to_process = [dict(r) for r in bq_client.query(query).result()]
    if not rows_to_process:
        return [], 0

    logger.info("Detected %d files needing chunking", len(rows_to_process))

    service_url = _resolve_service_url(project_id, region)
    logger.info(
        "Dispatching to %s with %d parallel workers (flush every %d chunks)",
        service_url,
        max_workers,
        flush_batch_size,
    )

    _prepare_chunks_staging(bq_client, fq_chunks_table, run_id)

    pending: list[dict] = []
    delete_pending: list[str] = []
    successful_file_ids: list[str] = []
    total_chunks = 0
    total_failed_enrichments = 0
    failures = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_row = {
            pool.submit(
                _process_one,
                service_url,
                row,
                chunk_size,
                chunk_overlap,
            ): row
            for row in rows_to_process
        }
        for fut in as_completed(future_to_row):
            row = future_to_row[fut]
            try:
                reply = fut.result()
                file_chunks = _build_chunk_rows(row, reply)
                pending.extend(file_chunks)
                # Old chunks for this file get deleted only AFTER the
                # service confirmed success, so a chunk-index outage
                # leaves the previous chunks alive (stale results beat
                # empty results — search keeps working for the file).
                delete_pending.append(row["file_id"])
                total_chunks += len(file_chunks)
                total_failed_enrichments += int(
                    reply.get("failed_enrichments", 0) or 0
                )
                successful_file_ids.append(row["file_id"])
            except Exception as e:
                failures += 1
                logger.error(
                    "chunk-index failed for %s: %s",
                    row["gcs_uri"],
                    e,
                )
            completed += 1
            if len(pending) >= flush_batch_size:
                _append_chunks_batch(
                    bq_client, fq_chunks_table, pending, run_id
                )
                pending = []
            # Flush deletes in batches so we don't issue one DELETE per
            # successful file (~16 chunks each ⇒ thousands of small
            # deletes per run otherwise).
            if len(delete_pending) >= DELETE_BATCH_SIZE:
                bq_utils.delete_by_file_ids(
                    bq_client, fq_chunks_table, delete_pending
                )
                delete_pending = []
            if completed % 50 == 0 or completed == len(rows_to_process):
                logger.info(
                    "chunk-index progress: %d/%d (%d failed)",
                    completed,
                    len(rows_to_process),
                    failures,
                )

    if pending:
        _append_chunks_batch(bq_client, fq_chunks_table, pending, run_id)
    if delete_pending:
        bq_utils.delete_by_file_ids(bq_client, fq_chunks_table, delete_pending)

    _flush_chunks_staging(bq_client, fq_chunks_table, run_id)

    enrichment_pct = (
        100.0 * total_failed_enrichments / total_chunks if total_chunks else 0.0
    )
    logger.info(
        "Inserted %d chunks for %d files (%d failures, %d empty-context "
        "chunks = %.1f%%)",
        total_chunks,
        len(successful_file_ids),
        failures,
        total_failed_enrichments,
        enrichment_pct,
    )
    return successful_file_ids, total_chunks


def run(
    bq_client: bigquery.Client,
    collection_path: str,
    documents_collection_path: str,
    fq_preprocessed_table: str,
    fq_chunks_table: str,
    chunk_size: int,
    chunk_overlap: int,
    vs_batch_size: int = 250,
    rechunk_all: bool = False,
) -> int:
    """Execute the full chunk-and-index step.

    Main entry point, called by the KFP chunk-and-index component.

    Both VS2 collections are populated by the chunk-index Cloud Run
    service per-file as part of the same request — by the time this
    function returns, every successful file_id is searchable in VS2 and
    its chunks live in the BQ chunks table.

    Args:
        bq_client: Authenticated BigQuery client.
        collection_path: Full VS2 chunks collection resource path.
            Used only for the ensure_collection bootstrap; the service
            itself reads its target collection from env vars.
        documents_collection_path: Full VS2 documents collection resource
            path. Same purpose as collection_path.
        fq_preprocessed_table: Fully-qualified BQ preprocessed table name.
        fq_chunks_table: Fully-qualified BQ chunks table name.
        chunk_size: Maximum characters per chunk.
        chunk_overlap: Overlapping characters between adjacent chunks.
        vs_batch_size: Reserved for backwards compat — the service uses
            its own VS_BATCH_SIZE env var.
        rechunk_all: If True, re-chunk all files regardless of timestamps.

    Returns:
        Number of chunks indexed into VS2 (0 if nothing to do).
    """
    del vs_batch_size  # signature-compat; service controls its own batch size

    project_id = bq_client.project
    region = bq_client.location

    # Ensure both VS2 collections exist (creates if not found). Done from
    # the orchestrator so the service stays a thin per-file worker.
    vs_utils.ensure_collection(
        project_id,
        region,
        collection_path.rsplit("/", 1)[-1],
        pipeline_config.vs_embedding_model,
        pipeline_config.vs_embedding_dims,
    )
    vs_utils.ensure_documents_collection(
        project_id,
        region,
        documents_collection_path.rsplit("/", 1)[-1],
        pipeline_config.vs_embedding_model,
        pipeline_config.vs_embedding_dims,
    )

    successful_file_ids, total_chunks = chunk_and_index_remote(
        bq_client,
        project_id,
        region,
        fq_preprocessed_table,
        fq_chunks_table,
        chunk_size,
        chunk_overlap,
        rechunk_all=rechunk_all,
    )

    if not successful_file_ids:
        logger.info("No files needed chunking.")
        return 0

    return total_chunks
