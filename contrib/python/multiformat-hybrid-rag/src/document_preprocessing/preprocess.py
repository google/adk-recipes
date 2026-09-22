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

"""Preprocess step: detect new/changed files in GCS and extract text.

Detects which files are new or changed by comparing the BQ Object Table
(metadata mirror of the GCS bucket) against our `preprocessed` table,
then dispatches text extraction in parallel directly to the Cloud Run
service via authenticated HTTP — no BQ remote function involved.

Why not BQ remote functions:
    The original design used a BQ REMOTE FUNCTION wrapping the Cloud Run
    service. BQ allocates parallelism based on data size, so for small
    inputs (~1k rows) it serializes calls onto a single slot, blocking
    that slot for the entire HTTP round-trip. With ~30s/file Gemini
    latency, 1k files took 6+ hours. Calling Cloud Run directly with a
    Python ThreadPoolExecutor gives us deterministic N-way parallelism
    and lets Cloud Run's own autoscaler do its job.

Change detection:
    Object Table LEFT JOIN preprocessed
    - New files:     prep.file_id IS NULL
    - Changed files: obj.md5_hash != prep.content_hash

Content dedup (two layers):
    - Cross-run: same md5_hash already extracted? Reference that file_id.
    - Within-run: multiple URIs share md5_hash this batch? Pick the
      lowest URI; the rest reference the picked file_id.
    Both produce stub rows (content='', error='duplicate_of:<id>') that
    satisfy the change-detection WHERE clause on later runs.

Identity:
    file_id = MD5(gcs_uri) — stable across content changes, tied to the
    file's location rather than its contents.

Supported file types:
    PDF, DOCX, DOC, PPTX, PPT, XLSX, XLS, RTF, HTML, JSON, JSONL, Markdown,
    plain text. This list, the extension filter in the change-detection
    query below, and PARSEABLE_MIMES in
    src/document_preprocessing/parser/__init__.py must stay in step — a
    format missing from any one of them is silently never ingested.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from google.cloud import bigquery, run_v2

from src.utils import bq_utils
from src.utils.auth import get_id_token
from src.utils.http_utils import post_with_retry

logger = logging.getLogger(__name__)

PREPROCESS_SERVICE_NAME = os.getenv(
    "PREPROCESS_SERVICE_NAME", "multiformat-hybrid-rag-preprocess"
)
DEFAULT_MAX_WORKERS = int(os.getenv("PREPROCESS_MAX_WORKERS", "200"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("PREPROCESS_HTTP_TIMEOUT", "600"))
HTTP_RETRY_ATTEMPTS = int(os.getenv("PREPROCESS_HTTP_RETRIES", "4"))
# How many extracted rows to accumulate before flushing to the BQ staging
# table. Larger = fewer load jobs (cheaper); smaller = lower peak memory.
# 100 keeps each flush well under the 10MB-per-load-job sweet spot for
# typical document text size.
FLUSH_BATCH_SIZE = int(os.getenv("PREPROCESS_FLUSH_BATCH_SIZE", "100"))


def compute_file_id(gcs_uri: str) -> str:
    """Derive a deterministic file identifier from a GCS URI.

    Uses MD5 hex digest so the same file always gets the same ID
    regardless of content changes, and the ID can be computed both in
    Python (hashlib) and in BigQuery (TO_HEX(MD5(uri))) to join across
    systems.
    """
    return hashlib.md5(gcs_uri.encode()).hexdigest()


def _resolve_service_url(project_id: str, region: str) -> str:
    """Look up the Cloud Run service endpoint via the Run admin API.

    We prefer this over hard-coding because the Cloud Run hash component
    in the URL is project-specific. Env var `PREPROCESS_SERVICE_URL`
    short-circuits the lookup (useful for local testing).
    """
    override = os.getenv("PREPROCESS_SERVICE_URL")
    if override:
        return override
    client = run_v2.ServicesClient()
    name = f"projects/{project_id}/locations/{region}/services/{PREPROCESS_SERVICE_NAME}"
    return client.get_service(name=name).uri


def _process_one(service_url: str, gcs_uri: str) -> dict:
    """Send one URI to the Cloud Run service, return parsed reply dict.

    Uses the same BQ-remote-function payload format the service already
    speaks ({"calls": [["uri"]]} → {"replies": ["json_str"]}) so the
    service code is unchanged.

    Calls `get_id_token` per request — the helper caches the token per
    audience and refreshes ~10 min before the 60-min expiry, so long
    runs don't hit silent 401 storms when the original token expires.
    """
    reply = post_with_retry(
        service_url,
        headers={
            "Authorization": f"Bearer {get_id_token(service_url)}",
            "Content-Type": "application/json",
        },
        body={"calls": [[gcs_uri]]},
        timeout=HTTP_TIMEOUT_SECONDS,
        attempts=HTTP_RETRY_ATTEMPTS,
    )
    return json.loads(reply["replies"][0])


def find_changed_with_dedup(
    bq_client: bigquery.Client,
    fq_object_table: str,
    fq_preprocessed_table: str,
    gcs_prefix: str,
) -> tuple[list[dict], list[dict]]:
    """Return (to_extract, dup_stubs).

    to_extract: files that need a Cloud Run call.
    dup_stubs: files whose content_hash already maps to a previously
        extracted file (cross-run) or to another file in this batch
        (within-run). Each stub carries the original `dup_of` file_id.

    Both lists carry: file_id, gcs_uri, md5_hash, file_name (+ dup_of
    for stubs).

    The query is structured as a sequence of named CTEs:
      gcs_files       — every file currently in the bucket prefix
      existing        — minimal cols from preprocessed for change detection
      dedup_index     — content_hash → file_id map of successful extractions
      changed         — files that are new or whose md5 changed
      with_dedup_info — changed rows enriched with cross-run / in-batch dup info
    """
    query = f"""
    WITH
      -- Step 1 — gcs_files: every file currently in the bucket prefix
      -- whose extension we can actually parse. The extension allowlist
      -- keeps unsupported types (media, images, archives, .DS_Store,
      -- etc.) out of the pipeline entirely — no HTTP call to Cloud Run,
      -- no Gemini call, no stub row written. Adding a new garbage type
      -- to the bucket is harmless: the regex won't match.
      --
      -- Keep this allowlist in step with the module docstring and with
      -- PARSEABLE_MIMES in src/document_preprocessing/parser/__init__.py.
      -- A format missing from any one of the three is silently dropped.
      -- A deployment that needs to exclude a noisy source (large
      -- machine-generated JSON, scrape index files, and so on) should
      -- filter it out of the bucket prefix rather than narrowing this
      -- allowlist, so the recipe keeps matching what its README promises.
      --
      -- file_id is the deterministic identity (MD5 of the URI) so we can
      -- join against `preprocessed.file_id` without storing it on the
      -- Object Table.
      gcs_files AS (
        SELECT
          uri AS gcs_uri,
          md5_hash,
          REGEXP_EXTRACT(uri, r'[^/]+$') AS file_name,
          TO_HEX(MD5(uri)) AS file_id
        FROM `{fq_object_table}`
        WHERE STARTS_WITH(uri, 'gs://')
          AND STRPOS(uri, '{gcs_prefix}') > 0
          -- Keep in step with PARSEABLE_MIMES in
          -- src/document_preprocessing/parser/__init__.py. Anything parseable
          -- but absent here is silently never ingested.
          AND REGEXP_CONTAINS(LOWER(uri),
                r'\\.(html?|pdf|docx?|pptx?|xlsx?|rtf|md|txt|jsonl?)$')
      ),

      -- Step 2 — existing: minimal projection of preprocessed used only
      -- for change detection (file_id + content_hash). Cheap to scan even
      -- on huge tables because the heavy `content` column is excluded.
      existing AS (
        SELECT file_id, content_hash
        FROM `{fq_preprocessed_table}`
      ),

      -- Step 3 — dedup_index: a content_hash → file_id map built from
      -- successful prior extractions. Filter `content_length > 0 AND
      -- error IS NULL` excludes stubs (scanner probes, dup stubs, errored
      -- rows) so we never pick a stub as a dedup target. Using
      -- `content_length` instead of `LENGTH(content)` keeps this scan
      -- cheap (8 bytes/row vs full content column).
      dedup_index AS (
        SELECT content_hash, ANY_VALUE(file_id) AS dup_file_id
        FROM `{fq_preprocessed_table}`
        WHERE content_length > 0 AND error IS NULL
        GROUP BY content_hash
      ),

      -- Step 4 — changed: files that need (re-)extraction. A file is
      -- "changed" if it's brand-new (no existing row) or its md5 differs
      -- from what we extracted last time.
      changed AS (
        SELECT g.*
        FROM gcs_files AS g
        LEFT JOIN existing AS e USING (file_id)
        WHERE e.file_id IS NULL OR e.content_hash != g.md5_hash
      ),

      -- Step 5 — with_dedup_info: enrich each changed file with two
      -- dedup signals so the Python caller can split rows into
      -- `to_extract` vs `dup_stubs`:
      --   cross_run_dup_file_id — an earlier run already extracted this
      --     md5 under a different file_id; reuse it (no Cloud Run call).
      --   rn / in_batch_first_file_id — multiple files in THIS batch
      --     share the same md5; only the lowest URI extracts, the rest
      --     reference it.
      with_dedup_info AS (
        SELECT
          c.file_id,
          c.gcs_uri,
          c.md5_hash,
          c.file_name,
          d.dup_file_id AS cross_run_dup_file_id,
          ROW_NUMBER()  OVER (PARTITION BY c.md5_hash ORDER BY c.gcs_uri) AS rn,
          FIRST_VALUE(c.file_id) OVER (PARTITION BY c.md5_hash ORDER BY c.gcs_uri) AS in_batch_first_file_id
        FROM changed AS c
        LEFT JOIN dedup_index AS d ON d.content_hash = c.md5_hash
      )

    SELECT * FROM with_dedup_info
    """
    rows = list(bq_client.query(query).result())
    to_extract: list[dict] = []
    dup_stubs: list[dict] = []
    for r in rows:
        base = {
            "file_id": r["file_id"],
            "gcs_uri": r["gcs_uri"],
            "md5_hash": r["md5_hash"],
            "file_name": r["file_name"],
        }
        if r["cross_run_dup_file_id"] is not None:
            dup_stubs.append({**base, "dup_of": r["cross_run_dup_file_id"]})
        elif r["rn"] > 1:
            dup_stubs.append({**base, "dup_of": r["in_batch_first_file_id"]})
        else:
            to_extract.append(base)
    return to_extract, dup_stubs


def _build_row(item: dict, parsed: dict) -> dict:
    """Shape a BQ upsert row from an extraction reply."""
    file_name = item["file_name"]
    file_type = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    text = parsed["text"]
    error = parsed.get("error")
    return {
        "file_id": parsed["file_id"],
        "gcs_uri": item["gcs_uri"],
        "content_hash": "" if error else item["md5_hash"],
        "content": text,
        "content_length": len(text),
        "file_name": file_name,
        "file_type": file_type,
        "relevant": parsed["relevant"],
        "error": error,
    }


def _build_dup_row(stub: dict) -> dict:
    """Shape a BQ upsert row for a content-duplicate stub (no extraction)."""
    file_name = stub["file_name"]
    file_type = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    return {
        "file_id": stub["file_id"],
        "gcs_uri": stub["gcs_uri"],
        "content_hash": stub["md5_hash"],
        "content": "",
        "content_length": 0,
        "file_name": file_name,
        "file_type": file_type,
        "relevant": False,
        "error": f"duplicate_of:{stub['dup_of']}",
    }


def preprocess_remote(
    bq_client: bigquery.Client,
    project_id: str,
    region: str,
    gcs_prefix: str,
    fq_object_table: str,
    fq_preprocessed_table: str,
    max_workers: int = DEFAULT_MAX_WORKERS,
    flush_batch_size: int = FLUSH_BATCH_SIZE,
) -> dict:
    """Detect changed files and extract text via parallel HTTP fanout.

    Streams results to a BQ staging table in batches so the orchestrator
    never holds more than `flush_batch_size` extracted rows in memory at
    once. After all results are flushed, a single MERGE upserts staging
    into the target preprocessed table by file_id.

    Wall-clock time ~ ceil(num_files / max_workers) x avg_latency_per_file.

    Returns a stats dict: total, relevant, duplicates, errors.
    """
    # Per-run staging suffix: prevents two concurrent pipeline runs from
    # clobbering each other's staging table. Combines wallclock seconds
    # (sortable / debuggable) with a random suffix (uniqueness).
    run_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    logger.info("preprocess run_id=%s", run_id)

    to_extract, dup_stubs = find_changed_with_dedup(
        bq_client, fq_object_table, fq_preprocessed_table, gcs_prefix
    )
    logger.info(
        "Detected %d files to extract, %d content duplicates",
        len(to_extract),
        len(dup_stubs),
    )

    stats = {
        "total": 0,
        "relevant": 0,
        "duplicates": len(dup_stubs),
        "errors": 0,
    }

    if not to_extract and not dup_stubs:
        return stats

    bq_utils.prepare_preprocessed_staging(
        bq_client, fq_preprocessed_table, run_id
    )

    if dup_stubs:
        dup_rows = [_build_dup_row(s) for s in dup_stubs]
        bq_utils.append_preprocessed_batch(
            bq_client, fq_preprocessed_table, dup_rows, run_id
        )
        stats["total"] += len(dup_rows)

    if to_extract:
        service_url = _resolve_service_url(project_id, region)
        logger.info(
            "Dispatching to %s with %d parallel workers (flush every %d)",
            service_url,
            max_workers,
            flush_batch_size,
        )

        pending: list[dict] = []
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_item = {
                pool.submit(_process_one, service_url, item["gcs_uri"]): item
                for item in to_extract
            }
            for fut in as_completed(future_to_item):
                item = future_to_item[fut]
                try:
                    parsed = fut.result()
                except Exception as e:
                    logger.error("HTTP failure for %s: %s", item["gcs_uri"], e)
                    parsed = {
                        "file_id": item["file_id"],
                        "text": "",
                        "relevant": False,
                        "error": f"http:{type(e).__name__}: {e}",
                    }
                row = _build_row(item, parsed)
                pending.append(row)
                if row.get("relevant"):
                    stats["relevant"] += 1
                if row.get("error"):
                    stats["errors"] += 1
                completed += 1
                if len(pending) >= flush_batch_size:
                    bq_utils.append_preprocessed_batch(
                        bq_client, fq_preprocessed_table, pending, run_id
                    )
                    stats["total"] += len(pending)
                    pending = []
                if completed % 50 == 0 or completed == len(to_extract):
                    logger.info(
                        "Extraction progress: %d/%d", completed, len(to_extract)
                    )

        if pending:
            bq_utils.append_preprocessed_batch(
                bq_client, fq_preprocessed_table, pending, run_id
            )
            stats["total"] += len(pending)

    bq_utils.merge_preprocessed_staging(
        bq_client, fq_preprocessed_table, run_id
    )
    return stats


def run(
    bq_client: bigquery.Client,
    project_id: str,
    region: str,
    gcs_prefix: str,
    fq_object_table: str,
    fq_preprocessed_table: str,
) -> int:
    """Execute the full preprocess step: detect → extract → stream upsert.

    Returns the total number of rows written. Main entry point, called by
    the KFP preprocess component.
    """
    stats = preprocess_remote(
        bq_client,
        project_id,
        region,
        gcs_prefix,
        fq_object_table,
        fq_preprocessed_table,
    )

    if stats["total"] == 0:
        logger.info("No new or changed files detected.")
        return 0

    logger.info(
        "Preprocess complete: %d rows (%d relevant, %d duplicates, %d errors)",
        stats["total"],
        stats["relevant"],
        stats["duplicates"],
        stats["errors"],
    )
    return stats["total"]
