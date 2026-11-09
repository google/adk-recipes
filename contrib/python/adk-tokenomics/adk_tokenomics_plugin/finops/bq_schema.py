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

"""
Tokenomics SDK - BigQuery Schema Auto-Provisioner
Provisions partitioned and clustered BigQuery telemetry tables and FinOps views.
"""

from adk_tokenomics_plugin.core.config import (
    validate_bq_identifier,
    validate_bq_project_id,
)

ADK_AGENT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS `{project_id}.{dataset_id}.{table_id}` (
    timestamp TIMESTAMP NOT NULL,
    event_type STRING,
    agent STRING,
    session_id STRING,
    user_id STRING,
    invocation_id STRING,
    content STRING,
    attributes STRING,
    latency_ms STRING
)
PARTITION BY DATE(timestamp)
CLUSTER BY agent, session_id;
"""

ADK_LLM_RESPONSE_VIEW_DDL = """
CREATE VIEW IF NOT EXISTS `{project_id}.{dataset_id}.{view_id}` AS
SELECT
    timestamp,
    event_type,
    agent,
    session_id,
    invocation_id,
    COALESCE(
        JSON_VALUE(attributes, '$.model_version'),
        JSON_VALUE(attributes, '$.model'),
        JSON_VALUE(content, '$.model_version'),
        'gemini-3.8-flash'
    ) AS model_version,
    COALESCE(
        SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.promptTokens') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_prompt_tokens') AS INT64),
        0
    ) AS usage_prompt_tokens,
    COALESCE(
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64),
        0
    ) AS usage_tool_use_tokens,
    COALESCE(
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_cached_tokens') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cachedContentTokenCount') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cacheReadTokens') AS INT64),
        0
    ) AS usage_cached_tokens,
    COALESCE(
        SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_completion_tokens') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidatesTokenCount') AS INT64),
        0
    ) AS usage_completion_tokens,
    COALESCE(
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_thinking_tokens') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughtsTokenCount') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64),
        0
    ) AS usage_thinking_tokens,
    COALESCE(
        SAFE_CAST(JSON_VALUE(content, '$.usage.total') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_total_tokens') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.total_token_count') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.totalTokenCount') AS INT64),
        SAFE_CAST(JSON_VALUE(content, '$.usage.total_tokens') AS INT64),
        COALESCE(
            SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.promptTokens') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_prompt_tokens') AS INT64),
            0
        ) + COALESCE(
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64),
            0
        ) + COALESCE(
            SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_completion_tokens') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidatesTokenCount') AS INT64),
            0
        ) + COALESCE(
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_thinking_tokens') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughtsTokenCount') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thinkingOutputTokens') AS INT64),
            0
        ),
        0
    ) AS usage_total_tokens,
    SAFE_DIVIDE(
        COALESCE(
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_cached_tokens') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cached_content_token_count') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cachedContentTokenCount') AS INT64),
            SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.cacheReadTokens') AS INT64),
            0
        ),
        NULLIF(
            COALESCE(
                SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64),
                SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64),
                SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.promptTokens') AS INT64),
                SAFE_CAST(JSON_VALUE(attributes, '$.usage_prompt_tokens') AS INT64),
                0
            ) + COALESCE(
                SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.tool_use_prompt_token_count') AS INT64),
                SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.toolUsePromptTokenCount') AS INT64),
                SAFE_CAST(JSON_VALUE(attributes, '$.usage_tool_use_tokens') AS INT64),
                0
            ),
            0
        )
    ) AS context_cache_hit_rate,
    COALESCE(
        SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64),
        SAFE_CAST(JSON_VALUE(latency_ms, '$') AS INT64),
        SAFE_CAST(ROUND(SAFE_CAST(TRIM(TO_JSON_STRING(latency_ms), '"') AS FLOAT64)) AS INT64),
        0
    ) AS total_ms,
    latency_ms,
    JSON_VALUE(content, '$.user_query') AS user_query,
    JSON_VALUE(content, '$.response') AS agent_response,
    COALESCE(JSON_VALUE(attributes, '$.sub_agent'), agent) AS sub_agent,
    COALESCE(JSON_VALUE(attributes, '$.orchestration_flow'), '') AS orchestration_flow,
    attributes,
    content
FROM `{project_id}.{dataset_id}.{table_id}`
WHERE event_type = 'LLM_RESPONSE' OR event_type IS NULL;
"""

V_LLM_RESPONSE_DDL = ADK_LLM_RESPONSE_VIEW_DDL

BIGQUERY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS `{project_id}.{dataset_id}.{table_id}` (
    timestamp TIMESTAMP NOT NULL,
    session_id STRING,
    app_name STRING,
    model_name STRING,
    user_query STRING,
    agent_response STRING,
    input_tokens INT64,
    cached_tokens INT64,
    output_tokens INT64,
    thinking_tokens INT64,
    total_cost FLOAT64,
    tools_called ARRAY<STRING>,
    skills_active ARRAY<STRING>,
    raw_json STRING
)
PARTITION BY DATE(timestamp)
CLUSTER BY app_name, model_name, session_id;
"""


def provision_bigquery_table(
    project_id: str,
    dataset_id: str = "bq_adk_ds",
    table_id: str = "adk_agent_events",
    view_id: str = "v_llm_response",
    location: str = "US",
    legacy_flat_schema: bool = False,
) -> bool:
    """Creates the BigQuery dataset, partitioned telemetry table, and v_llm_response view if they do not exist."""
    try:
        project_id = validate_bq_project_id(project_id)
        dataset_id = validate_bq_identifier(dataset_id, "dataset_id")
        table_id = validate_bq_identifier(table_id, "table_id")
        if view_id and not legacy_flat_schema:
            view_id = validate_bq_identifier(view_id, "view_id")
    except ValueError as ve:
        print(
            f"[ERROR] Invalid BigQuery identifier for provisioning: {ve}",
            flush=True,
        )
        return False

    try:
        from google.cloud import bigquery
    except ImportError as ie:
        print(
            f"[ERROR] google-cloud-bigquery is not installed ({ie}). "
            "Install with: pip install 'adk-tokenomics-plugin[bigquery]'",
            flush=True,
        )
        return False

    try:
        client = bigquery.Client(project=project_id)

        # 1. Create Dataset if missing
        dataset_ref = bigquery.Dataset(f"{project_id}.{dataset_id}")
        dataset_ref.location = location
        try:
            client.create_dataset(dataset_ref, exists_ok=True)
            print(f"[OK] Dataset {project_id}.{dataset_id} ready.", flush=True)
        except Exception as e:
            print(f"[INFO] Dataset check: {e}", flush=True)

        # 2. Create Partitioned Table (ADK event schema by default; flat schema only when legacy_flat_schema=True)
        if legacy_flat_schema:
            ddl = BIGQUERY_TABLE_DDL.format(
                project_id=project_id, dataset_id=dataset_id, table_id=table_id
            )
        else:
            ddl = ADK_AGENT_EVENTS_DDL.format(
                project_id=project_id, dataset_id=dataset_id, table_id=table_id
            )
        client.query(ddl).result()
        print(
            f"[OK] Table {project_id}.{dataset_id}.{table_id} partitioned & clustered successfully.",
            flush=True,
        )

        # 3. Provision v_llm_response Analytics View via CREATE VIEW IF NOT EXISTS when using ADK schema
        if not legacy_flat_schema and view_id:
            view_ddl = ADK_LLM_RESPONSE_VIEW_DDL.format(
                project_id=project_id,
                dataset_id=dataset_id,
                table_id=table_id,
                view_id=view_id,
            )
            client.query(view_ddl).result()
            print(
                f"[OK] Analytics View {project_id}.{dataset_id}.{view_id} ready.",
                flush=True,
            )

        return True
    except Exception as e:
        print(f"[ERROR] Failed to provision BigQuery table: {e}", flush=True)
        return False
