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

"""BigQuery utility functions for dataset, table management and change detection."""

from __future__ import annotations

import logging

from google.cloud import bigquery

logger = logging.getLogger(__name__)


def get_client(project_id: str, location: str) -> bigquery.Client:
    return bigquery.Client(project=project_id, location=location)


def ensure_dataset(
    client: bigquery.Client, project_id: str, dataset_id: str, location: str
) -> None:
    """Create dataset if it doesn't exist."""
    dataset_ref = f"{project_id}.{dataset_id}"
    dataset = bigquery.Dataset(dataset_ref)
    dataset.location = location
    client.create_dataset(dataset, exists_ok=True)
    logger.info("Dataset ready: %s", dataset_ref)


def create_object_table(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
    table_id: str,
    connection_path: str,
    gcs_uri_prefix: str,
) -> None:
    """Create a BQ Object Table linked to a GCS bucket prefix."""
    fq_table = f"`{project_id}.{dataset_id}.{table_id}`"
    ddl = f"""
    CREATE OR REPLACE EXTERNAL TABLE {fq_table}
    WITH CONNECTION `{connection_path}`
    OPTIONS (
        object_metadata = 'SIMPLE',
        uris = ['{gcs_uri_prefix}*']
    )
    """
    client.query(ddl).result()
    logger.info(
        "Object table ready: %s.%s.%s", project_id, dataset_id, table_id
    )


def create_preprocessed_table(client: bigquery.Client, fq_table: str) -> None:
    """Create the preprocessed table if it doesn't exist.

    CLUSTER BY file_id: every per-run query (change-detection JOIN, MERGE
    upsert, cascade DELETE) joins or filters by file_id. Clustering means
    BigQuery only scans the data blocks that contain the file_ids we
    actually care about instead of the whole table. Critical for the
    end-of-run MERGE, which would otherwise scan the heavy `content`
    column on every row.
    """
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{fq_table}` (
        file_id        STRING NOT NULL,
        gcs_uri        STRING NOT NULL,
        content_hash   STRING NOT NULL,
        content        STRING NOT NULL,
        content_length INT64  NOT NULL,
        file_name      STRING,
        file_type      STRING,
        relevant       BOOL   NOT NULL,
        extracted_at   TIMESTAMP NOT NULL,
        error          STRING
    )
    CLUSTER BY file_id
    OPTIONS (
        description = 'Extracted text from source documents in GCS'
    )
    """
    client.query(ddl).result()
    logger.info("Preprocessed table ready: %s", fq_table)


def create_chunks_table(client: bigquery.Client, fq_table: str) -> None:
    """Create the chunks table if it doesn't exist.

    CLUSTER BY file_id: every per-run query (rechunk detection's
    `last_indexed_per_file` CTE, the pre-delete before staging, and the
    cleanup cascade DELETE) groups or filters by file_id. Clustering keeps
    a file's ~16 chunks adjacent on disk so these queries scan only the
    relevant clusters instead of the full table.
    """
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{fq_table}` (
        chunk_id    STRING NOT NULL,
        file_id     STRING NOT NULL,
        gcs_uri     STRING NOT NULL,
        chunk_index INT64  NOT NULL,
        chunk_text  STRING NOT NULL,
        context     STRING,
        chunked_at  TIMESTAMP NOT NULL,
        indexed_at  TIMESTAMP
    )
    CLUSTER BY file_id
    OPTIONS (
        description = 'Chunked text ready for Vector Search indexing'
    )
    """
    client.query(ddl).result()
    logger.info("Chunks table ready: %s", fq_table)


def get_deleted_files(
    client: bigquery.Client,
    fq_object_table: str,
    fq_preprocessed_table: str,
) -> list[dict]:
    """Find files in preprocessed that no longer exist in the GCS bucket.

    Returns list of dicts: {file_id, gcs_uri}.
    """
    query = f"""
    -- live_gcs_uris: snapshot of every URI currently in the bucket
    -- (the Object Table is an external view of GCS metadata).
    WITH live_gcs_uris AS (
        SELECT uri
        FROM `{fq_object_table}`
    )
    -- Anti-join: a preprocessed row whose URI is missing from the live
    -- GCS snapshot was deleted from the bucket since last run.
    SELECT prep.file_id, prep.gcs_uri
    FROM `{fq_preprocessed_table}` AS prep
    LEFT JOIN live_gcs_uris AS live
        ON prep.gcs_uri = live.uri
    WHERE live.uri IS NULL
    """
    rows = client.query(query).result()
    results = [dict(row) for row in rows]
    logger.info("Found %d deleted files", len(results))
    return results


_PREPROCESSED_SCHEMA = [
    bigquery.SchemaField("file_id", "STRING"),
    bigquery.SchemaField("gcs_uri", "STRING"),
    bigquery.SchemaField("content_hash", "STRING"),
    bigquery.SchemaField("content", "STRING"),
    bigquery.SchemaField("content_length", "INTEGER"),
    bigquery.SchemaField("file_name", "STRING"),
    bigquery.SchemaField("file_type", "STRING"),
    bigquery.SchemaField("relevant", "BOOLEAN"),
    bigquery.SchemaField("error", "STRING"),
]


def _preprocessed_staging_name(fq_table: str, run_id: str) -> str:
    """Per-run staging name so concurrent pipeline runs don't clobber
    each other. If a run crashes the table is left behind under its
    run_id — easier to debug than a singleton table mysteriously full
    of half-finished work.
    """
    return f"{fq_table.rsplit('.', 1)[0]}._staging_preprocessed_{run_id}"


def prepare_preprocessed_staging(
    client: bigquery.Client, fq_table: str, run_id: str
) -> str:
    """Create/truncate the preprocessed staging table for this run.
    Returns its FQ name.

    Call once at the start of a run. Subsequent batch loads use
    `append_preprocessed_batch` which appends, so this gives us a clean
    slate guaranteed.
    """
    staging_table = _preprocessed_staging_name(fq_table, run_id)
    job_config = bigquery.LoadJobConfig(
        schema=_PREPROCESSED_SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    client.load_table_from_json(
        [], staging_table, job_config=job_config
    ).result()
    return staging_table


def append_preprocessed_batch(
    client: bigquery.Client, fq_table: str, rows: list[dict], run_id: str
) -> None:
    """Append a batch of rows to the preprocessed staging table.

    Use after `prepare_preprocessed_staging`. Free the rows from your
    in-memory buffer once this returns.
    """
    if not rows:
        return
    staging_table = _preprocessed_staging_name(fq_table, run_id)
    job_config = bigquery.LoadJobConfig(
        schema=_PREPROCESSED_SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
    client.load_table_from_json(
        rows, staging_table, job_config=job_config
    ).result()


def merge_preprocessed_staging(
    client: bigquery.Client, fq_table: str, run_id: str
) -> None:
    """MERGE staging → target (upsert by file_id) and drop staging.

    Idempotent on file_id. Call once at the end of a run, after all
    batches have been appended via `append_preprocessed_batch`.
    """
    staging_table = _preprocessed_staging_name(fq_table, run_id)
    merge_sql = f"""
    -- Upsert by file_id: each staging row either updates the existing
    -- preprocessed row (file changed) or inserts a new one (file is new).
    -- gcs_uri is immutable for a given file_id (file_id = MD5(uri)) so we
    -- never overwrite it on UPDATE.
    MERGE `{fq_table}` AS target
    USING `{staging_table}` AS source
    ON target.file_id = source.file_id
    WHEN MATCHED THEN
        UPDATE SET
            content_hash   = source.content_hash,
            content        = source.content,
            content_length = source.content_length,
            file_name      = source.file_name,
            file_type      = source.file_type,
            relevant       = source.relevant,
            error          = source.error,
            extracted_at   = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN
        INSERT (file_id, gcs_uri, content_hash, content, content_length,
                file_name, file_type, relevant, error, extracted_at)
        VALUES (source.file_id, source.gcs_uri, source.content_hash, source.content,
                source.content_length, source.file_name, source.file_type,
                source.relevant, source.error, CURRENT_TIMESTAMP())
    """
    client.query(merge_sql).result()
    client.delete_table(staging_table, not_found_ok=True)
    logger.info("Merged staging into %s", fq_table)


def delete_by_file_ids(
    client: bigquery.Client,
    fq_table: str,
    file_ids: list[str],
) -> int:
    """Delete rows where file_id IN (file_ids). Returns rows affected."""
    if not file_ids:
        return 0

    # UNNEST(@file_ids) lets us pass an array via parameter binding, so the
    # query plan is shared across runs regardless of the list contents.
    sql = f"""
    DELETE FROM `{fq_table}`
    WHERE file_id IN UNNEST(@file_ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("file_ids", "STRING", list(file_ids)),
        ]
    )
    result = client.query(sql, job_config=job_config).result()
    count = result.num_dml_affected_rows or 0
    logger.info("Deleted %d rows from %s", count, fq_table)
    return count
