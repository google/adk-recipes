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

"""One durable row per trajectory a sweep sampled, whatever became of it.

Wiring::

    BigQueryJobFetcher  core.nodes.init                 trajectory_tools, insight_tools,
        │ add(), flush()  │ delete_run_trajectories()   rca_tools
        ▼                 │                                 │
    TrajectoryRecorder ◄──┘                                 ▼
        ├──► build_archive()                            TrajectoryReader (Protocol)
        │ record(), record_payloads(),                      ▲ implements
        │ delete_run_trajectories()                         │
        ▼                                                   ├── InMemoryTrajectoryReader
    TrajectoryStore (Protocol)                          BigQueryTrajectoryReader
        ▲ implements                                        │ query
        ├── InMemoryTrajectoryStore                         ▼
    BigQueryTrajectoryStore ── load jobs, DELETE ─────► BigQuery

A **trajectory** is what the sweep judged as one thing: a whole session where
it reviewed the conversation, a single turn where it scored that. A **trace**
is smaller -- one turn's worth of spans -- so a session-scoped trajectory is
several traces bundled together. That is why `Trajectory.source_trace_ids` is a
list, and why this package is not called `traces`.

A trajectory that passed, that failed without being clustered, or that could
not be ingested at all leaves nothing behind in the insight tables, so a run's
telemetry is knowable only as the totals in its counters. This package names
each one instead: the ids it was built from, so a reader can link out to the
conversation, and how much of it arrived.

**Ingestion only.** A row is the state at the end of the fetch step, before
anything has judged the conversation. A sweep runs one analysis today and is
expected to run several, so no outcome is stored here -- the run counters hold
the aggregate, and a per-trajectory result, if one is ever kept, belongs in its
own table keyed back to this. What the reader calls an *outcome* is therefore
derived at read time, by joining the insight occurrences, and is never written
back.

**And the conversation itself**, beside the index: `TrajectoryPayload` and
`TrajectoryPayloadTurn` archive the assembled turns, so an insight that recurs
for months still has its evidence after the source telemetry has aged out. Two
grains, deliberately: a `Trajectory` is one *sweep's ingestion* and is keyed by
the run, while a payload is the *conversation* and is keyed without one, so the
bulky copy is stored once however many sweeps sampled it -- and so each can
have the retention it wants, which a single table could not, since BigQuery
expires partitions rather than columns.

See `models` for the vocabulary, `payloads` for how one assembled case becomes
rows, `store` for what a writer has to answer -- one store over all three
tables, the way `InsightStore` covers an insight and its occurrences -- and
`reader` for the reads the dashboard is built on. `bigquery_store` and
`bigquery_reader` are the deployed implementations of the two;
`standalone.InMemoryTrajectoryStore` and `InMemoryTrajectoryReader` are the
others.
"""
