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

"""Agent Runtime entrypoint for AQA.

Once deployed, the workflow is reachable through Agent Runtime's data
plane:
  - `:streamQuery` for the conversational orchestrator.
  - `:asyncQuery` to run an investigation, dispatched when the query starts
    with the `RUN_KEYWORD` marker (see `core.investigation.job_scheduling`).
"""

from google.adk.apps import App

from .core.orchestrator import build_orchestrator

root_agent = build_orchestrator()
app = App(name="aqa", root_agent=root_agent)
