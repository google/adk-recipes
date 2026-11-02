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

"""``RUN_KEYWORD``, the only surviving action prefix.

It survives as a keyword because the agent sends it to itself through a durable
query job whose payload has no field for a URL path. The ``before_agent``
callback matches this prefix and runs the investigation without the model.
"""

from __future__ import annotations

# Marks a self-issued investigation-run query: ``<RUN_KEYWORD> <run_id>``.
# `core.investigation.job_scheduling` submits a durable long-running job
# carrying this message against this same Agent Runtime agent (the "agent queries
# itself" pattern); `before_agent` recognizes it and runs the investigation
# graph via `core.investigation.job_execution`. It is the contract between the
# two halves of a run.
RUN_KEYWORD = "__RUN_INVESTIGATION__"
