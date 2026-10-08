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

"""Standalone storage implementation for AQuA: in memory, and local files.

Wiring::

    backends.configure_providers(), backends.override_providers()
        │ create_store(), build_bindings()
        ▼
    bindings ──► TrajectoryRecorder ──► InMemoryTrajectoryStore
        │ provider factories
        ▼                                          implements
    InMemoryInvestigationStore ──────────────────► InvestigationStore (Protocol)
    InMemoryInsightStore ────────────────────────► InsightStore (Protocol)
    InMemoryInsightReader ───────────────────────► InsightReader (Protocol)
    InMemoryTrajectoryStore ─────────────────────► TrajectoryStore (Protocol)
    InMemoryTrajectoryReader ────────────────────► TrajectoryReader (Protocol)
    InMemoryRootCauseStore ──────────────────────► RootCauseWriter (Protocol)
        │ save_run(), update_insights(), append_root_cause(), save_archive()
        ▼
    InMemoryStore

    FileObjectStore ─────────────────────────────► ObjectStore (Protocol)
        │ .aqua/job/<name>, .aqua/source/<name>

`InMemoryStore` holds execution state in memory without requiring external cloud
storage (such as BigQuery or Cloud Storage). Entity-specific adapters implement
the storage contracts used by the workflow graph and dashboard:
- Runs: `InMemoryInvestigationStore`
- Insights: `InMemoryInsightStore` and `InMemoryInsightReader`
- Trajectories: `InMemoryTrajectoryStore` and `InMemoryTrajectoryReader`
- Diagnoses: `InMemoryRootCauseStore`

`FileObjectStore` keeps what a deployment keeps in its jobs bucket -- the goal,
the memories and the attached agents' configurations -- as files under
`.aqua/job/`, and what it keeps in its source bucket -- the observed agents'
source snapshots -- under `.aqua/source/`. Both outlive the process.

`build_bindings` maps storage providers to these adapters, registered via
`backends.configure_providers()` when `AQA_STANDALONE=1`.

Named `standalone` rather than `local` because "local" refers to running the
dashboard locally against a deployed engine requiring `terraform apply`.
"""
