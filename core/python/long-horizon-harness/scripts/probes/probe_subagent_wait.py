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

"""Live-LLM smoke for ``agent(action='wait')`` (fleet next-completed).

Spawns two REAL child agents (real Gemini) in the background, then uses the new
``wait`` action to collect them one at a time and confirms the fleet drains.

Run:  (env from .env)  uv run python scripts/probes/probe_subagent_wait.py
Hits Vertex — needs GCP auth + GOOGLE_CLOUD_* set.
"""

from __future__ import annotations

import asyncio
import tempfile


async def main() -> None:
    from google.adk.environment import LocalEnvironment

    from app.environment_context import set_active_environment
    from app.subagents.registry import reset_registry
    from app.subagents.spawn import agent

    reset_registry()
    with tempfile.TemporaryDirectory() as tmp:
        set_active_environment(LocalEnvironment(working_dir=tmp))

        print("=== spawn two real children in the background ===")
        s1 = await agent(
            action="spawn",
            goal="Reply with exactly the single word: ONE. Do not call any tools.",
            tools=[],
            name="child-one",
        )
        s2 = await agent(
            action="spawn",
            goal="Reply with exactly the single word: TWO. Do not call any tools.",
            tools=[],
            name="child-two",
        )
        ids = {s1["task_id"], s2["task_id"]}
        print("spawned:", ids)

        print("\n=== wait for the FIRST to finish ===")
        w1 = await agent(action="wait", timeout_s=90.0)
        print("first done:", w1.get("task_id"), w1.get("status"))
        print("  still_running:", w1.get("still_running"))

        print("\n=== wait for the SECOND ===")
        w2 = await agent(action="wait", timeout_s=90.0)
        print("second done:", w2.get("task_id"), w2.get("status"))

        print("\n=== a third wait should report the drained fleet ===")
        w3 = await agent(action="wait", timeout_s=5.0)
        print("third wait:", w3.get("status"))

        finished = {w1.get("task_id"), w2.get("task_id")}
        ok = (
            finished == ids
            and w1.get("status") == "completed"
            and w2.get("status") == "completed"
            and w3.get("status") == "no_active_tasks"
        )
        print(
            f"\nRESULT: {'PASS' if ok else 'FAIL'} "
            f"(both children collected via wait; fleet drained cleanly)"
        )


if __name__ == "__main__":
    asyncio.run(main())
