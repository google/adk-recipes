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
"""Settings for the FinOps agent."""

import os

# Google-hosted endpoint, enabled together with the BigQuery API.
BIGQUERY_MCP_URL = "https://bigquery.googleapis.com/mcp"

# Allowlist, so the model never sees execute_sql or any other write tool.
READONLY_BIGQUERY_TOOLS = [
    "list_dataset_ids",
    "get_dataset_info",
    "list_table_ids",
    "get_table_info",
    "execute_sql_readonly",
    "get_query_results",
]

GCP_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

MCP_TIMEOUT_SECONDS = 30.0
MCP_SSE_READ_TIMEOUT_SECONDS = 120.0

# Sent as x-goog-user-project. Unset: the credentials' default project.
QUOTA_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

# Cloud Billing export table: project.dataset.table
BILLING_TABLE = os.getenv("BILLING_TABLE")


def billing_project(table: str | None, override: str | None = None) -> str:
    """Returns the project to run cost queries in, or "" if not configured.

    The export covers every project under the billing account, so queries run
    in the project that holds the export, not the one being asked about.
    """
    if override:
        return override
    if table and table.count(".") == 2:
        return table.split(".", maxsplit=1)[0]
    return ""


BILLING_PROJECT = billing_project(BILLING_TABLE, os.getenv("BILLING_PROJECT"))
