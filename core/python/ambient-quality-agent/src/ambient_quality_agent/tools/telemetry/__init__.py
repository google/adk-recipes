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

"""Chat tools that read the observed agent's telemetry table.

Wiring::

    chat model
        │ describe_telemetry()
        ▼
    describe ──► effective_config.load()
        │    ──► selector.build_selector_table_ref()
        │    ──► custom_investigation_skill.resolve_selector_recipes()
        ▼ worker thread
    _load_table_columns() ── client_factory() ──► bigquery.Client
        │                                            │
        ▼                                            │
    schema.load_telemetry_schema() ── get_table() ───┴──► BigQuery

* `describe` -- the `describe_telemetry` chat tool: returns the telemetry
  source, selector table, live columns, and skill reference for selector
  recipes.
* `schema` -- reads table columns from BigQuery metadata.
"""
