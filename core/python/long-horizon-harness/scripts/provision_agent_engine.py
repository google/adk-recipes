#!/usr/bin/env python3
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

"""Discover-or-create the shared Vertex Agent Engine.

A single Agent Engine hosts both Memory Bank (for cross-session memory) and
the per-user sandboxes — the engine is just a parent resource and supports
both attached features. Provisioning it once at deploy time avoids the
first-request latency of a lazy discover-or-create in the request path.

Designed to be invoked by terraform's `external` data source — reads a JSON
query object from stdin, writes a JSON result object to stdout. Idempotent:
re-running returns the same resource name as long as the display_name matches.

Stdin contract:
    {"project_id": "<gcp-project>", "location": "us-central1",
     "display_name": "lha-agent-engine"}

Stdout contract:
    {"name": "projects/.../locations/.../reasoningEngines/..."}

All diagnostic output must go to stderr — terraform parses stdout as JSON.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import vertexai
from vertexai._genai.types import (
    AgentEngineConfig,
    ReasoningEngineContextSpec,
)

from app.infrastructure.memory_config import memory_bank_config


def _structured_dump(spec: object) -> list[Any]:
    """Normalized structured_memory_configs of a context_spec, defensively.

    Comparing the full dump (not just schema ids) means a schema *shape* or
    ``scope_keys`` change triggers an update, while an already-converged engine
    skips the PATCH so ``terraform apply`` stays a no-op.
    """
    bank = getattr(spec, "memory_bank_config", None)
    out: list[Any] = []
    for cfg in getattr(bank, "structured_memory_configs", None) or []:
        dump = getattr(cfg, "model_dump", None)
        out.append(dump(exclude_none=True) if callable(dump) else cfg)
    return out


def main() -> None:
    query = json.load(sys.stdin)
    project_id = query["project_id"]
    location = query["location"]
    display_name = query["display_name"]

    print(
        f"provision_agent_engine: project={project_id} location={location} "
        f"display_name={display_name}",
        file=sys.stderr,
    )

    client = vertexai.Client(project=project_id, location=location)

    matching = [
        a
        for a in client.agent_engines.list()
        if a.api_resource.display_name == display_name
    ]
    desired_spec = ReasoningEngineContextSpec(
        memory_bank_config=memory_bank_config
    )
    desired_structured = _structured_dump(desired_spec)

    if matching:
        engine = matching[0]
        print(
            f"provision_agent_engine: reusing existing {engine.api_resource.name}",
            file=sys.stderr,
        )
        # The memory_bank_config (managed topics + structured profile schema) is
        # only set at create time; apply structured-profile changes to an existing
        # engine here. The PATCH replaces the whole context_spec, so we send the
        # full config. (Managed-topic-only edits aren't diffed here.)
        existing_structured = _structured_dump(
            getattr(engine.api_resource, "context_spec", None)
        )
        if existing_structured != desired_structured:
            print(
                "provision_agent_engine: updating memory_bank_config "
                "(structured_memory_configs changed)",
                file=sys.stderr,
            )
            client.agent_engines.update(
                name=engine.api_resource.name,
                config=AgentEngineConfig(context_spec=desired_spec),
            )
    else:
        print(
            "provision_agent_engine: creating new Agent Engine with Memory Bank",
            file=sys.stderr,
        )
        engine = client.agent_engines.create(
            config=AgentEngineConfig(
                display_name=display_name,
                context_spec=desired_spec,
            ),
        )
        print(
            f"provision_agent_engine: created {engine.api_resource.name}",
            file=sys.stderr,
        )

    json.dump({"name": engine.api_resource.name}, sys.stdout)


if __name__ == "__main__":
    main()
