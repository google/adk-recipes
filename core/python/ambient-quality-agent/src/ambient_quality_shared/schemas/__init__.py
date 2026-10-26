# Copyright 2026 Google LLC
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

"""BigQuery schema files of the telemetry tables AQuA reads.

Each file, in `bq` JSON schema format, lists the columns one telemetry source's
ingestion queries require:

* `agent_events.schema.json`: `big_query`, the BigQuery Agent Analytics table.
* `all_spans.schema.json`: `cloud_ops`, the Cloud Trace span view. A trace dump
  ships it as the load schema of its spans, so it also lists the span events'
  `time`, which ingestion does not read.
* `gen_ai_client_inference_operation_details.schema.json`: `cloud_logging`, the
  log sink table.

`aqua attach` checks a table against the file of its source before storing the
attachment. The package holds data only, so the vendored CLI, which runs with
minimal dependencies, reads it too.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent
