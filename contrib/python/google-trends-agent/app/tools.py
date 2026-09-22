# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""BigQuery tool used by the Google Trends executor agent."""

import json
import logging
import os

from google.cloud import bigquery

logger = logging.getLogger(__name__)


def clean_sql_query(text: str) -> str:
    """Strips markdown fences and stray escapes from a generated SQL string."""
    return (
        text.replace("\\n", " ")
        .replace("\n", " ")
        .replace("\\", "")
        .replace("```sql", "")
        .replace("```", "")
        .strip()
    )


def execute_bigquery_sql(sql: str) -> str:
    """Executes a BigQuery SQL query and returns the result as a JSON string."""
    cleaned_sql = clean_sql_query(sql)
    logger.debug("Executing BigQuery SQL query: %s", cleaned_sql)
    try:
        # The client uses the GOOGLE_CLOUD_PROJECT environment variable.
        client = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
        query_job = client.query(cleaned_sql)  # Make an API request.
        results = query_job.result()  # Wait for the job to complete.

        # Convert RowIterator to a list of dictionaries
        sql_results = [dict(row) for row in results]

        # Return the results as a JSON string.
        if not sql_results:
            return "Query returned no results."
        # Use json.dumps for proper JSON formatting, handle non-serializable
        # types like datetime
        return (
            json.dumps(sql_results, default=str)
            .replace("```sql", "")
            .replace("```", "")
        )
    except Exception as e:
        return f"Error executing BigQuery query: {e!s}"
