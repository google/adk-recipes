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

"""Durable, deduplicated quality insights.

Wiring::

    insight_correlation ─┬─► merge_finding_sets()
      │                  ├─► cluster_and_label(), validate_clusters(), attach_examples()
      │                  └─► merge_clusters(), verify_clusters()
      │ find_existing_insights(),           insight_tools
      │ save_investigation_result(), ...      │ dismiss_insight(), merge_insight()
      ▼                                       ▼
    InsightWriter (Protocol) ◄── extends ── InsightStore (Protocol)
                                              ▲ implements
    match_candidates() ◄─────────────────── BigQueryInsightStore
                                              │ query, load jobs
                                              ▼
    RootCauseStore ─── load jobs ─────────► BigQuery ◄─ query ─ BigQueryInsightReader
      │ implements                                                │ implements
      ▼                                                           ▼
    RootCauseWriter (Protocol)                                  InsightReader (Protocol)
      ▲ save()                                                    ▲ list_insights(), ...
      │                                                           │
    root_cause_tools                          insight_tools, rca_tools, root_cause_tools

A sweep's findings are clustered into labeled candidate issues (`clustering`)
and correlated against what earlier sweeps recorded, so a recurring defect is
tracked as one insight with history rather than resurfacing as a new finding
every run. See `models` and `findings` for the vocabulary.
"""

from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.insights.bigquery_store import (
    BigQueryInsightStore,
)
from ambient_quality_agent.tools.insights.clustering import (
    Cluster,
    attach_examples,
    cluster_and_label,
    validate_clusters,
)
from ambient_quality_agent.tools.insights.findings import Finding, FindingSet
from ambient_quality_agent.tools.insights.merge import merge_clusters
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    InsightView,
    RubricExample,
    mint_id,
)
from ambient_quality_agent.tools.insights.reader import InsightReader
from ambient_quality_agent.tools.insights.store import InsightStore

__all__ = [
    "BigQueryInsightReader",
    "BigQueryInsightStore",
    "Cluster",
    "Finding",
    "FindingSet",
    "Insight",
    "InsightOccurrence",
    "InsightReader",
    "InsightStatus",
    "InsightStore",
    "InsightView",
    "RubricExample",
    "attach_examples",
    "cluster_and_label",
    "merge_clusters",
    "mint_id",
    "validate_clusters",
]
