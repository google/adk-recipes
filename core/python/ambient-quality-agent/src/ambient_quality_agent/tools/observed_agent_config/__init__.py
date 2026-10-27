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

"""The observed agent's configuration: how AQuA watches the agent.

AQuA's own settings come from the engine's environment. The observed agent's
settings come from an object stored per agent. No agent holds these modules as
tools: `agents-cli aqua attach` changes the configuration through command
routes, and every other caller only reads it::

    CLI attach / list-agents / detach
        │
        ▼
    core.command_routes ──► commands ──► store ──► gs://<jobs>/agents/<key>.json
                            │   │  │       ▲   ▲
                            │   │  │       │   │
                            │   │  └───────┼─► schedule ◄── ambient triggers, health
                            │   ▼          │
                            │  keys        │
                            │              │
                            ▼              │
                            effective_config ◄── investigations, routes,
                                                 ambient triggers,
                                                 documents.agent_scope

* `store` -- the stored object: `AgentRecord`, and reading, writing, listing
  and deleting it, keyed by the agent key. `documents.agent_scope` also asks
  it for the default agent directly, so that a failed read is not taken for
  no agent.
* `effective_config` -- the `Config` a request runs under: AQuA's own settings
  from the environment, with the stored object layered over the observed
  agent's half; the stored object of the agent a request selects; and the
  agents a tick or an engine's update is for.
* `commands` -- what attaching, listing and detaching do behind the command
  routes: merge, validate, store.
* `schedule` -- whether an agent's cron fell due since the previous scheduler
  tick, and which crons an attach accepts.
* `keys` -- which key an agent is stored under: its agent key, or the bare
  name an agent in AQuA's own project keeps.
"""
