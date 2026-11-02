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

"""System 2 (`system2_agent`) wrapper that drives coding-harness threads on a docker computer for `agents-cli eval run`."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.genai import types

from app.threads.model import MILESTONE_KINDS, ThreadEvent

from .runner import CaseRun

KEEPALIVE_S = 30.0
TOOL_NAME = "run_system2_case"


def harness() -> str:
    return (os.getenv("SYSTEM2_HARNESS") or "claude").strip().lower()


def parse_case(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The message carries no case spec (a JSON object).")
    return json.loads(text[start : end + 1])


def final_text(report: dict[str, Any]) -> str:
    lines = []
    for alias, thread in (report.get("threads") or {}).items():
        result = (
            thread.get("result_text")
            or thread.get("summary")
            or thread.get("error")
            or ""
        )
        lines.append(f"[{alias} {thread.get('state')}] {result}".strip())
    if report.get("runner_error"):
        lines.append(f"[runner error] {report['runner_error']}")
    return "\n\n".join(lines) or "No thread ran."


class System2CaseAgent(BaseAgent):
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parts = (
            ctx.user_content.parts
            if ctx.user_content and ctx.user_content.parts
            else []
        )
        spec = parse_case("".join(p.text or "" for p in parts))
        chosen = harness()

        def event(**kwargs: Any) -> Event:
            return Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                branch=ctx.branch,
                **kwargs,
            )

        call = types.FunctionCall(
            name=TOOL_NAME, args={**spec, "harness": chosen}
        )
        yield event(
            content=types.Content(
                role="model", parts=[types.Part(function_call=call)]
            )
        )
        milestones: asyncio.Queue[tuple[str, ThreadEvent]] = asyncio.Queue()

        def on_event(alias: str, thread_event: ThreadEvent) -> None:
            if thread_event.kind in MILESTONE_KINDS:
                milestones.put_nowait((alias, thread_event))

        root = Path(os.getenv("EVAL_RUN_DIR") or "artifacts/system2")
        run_dir = root / "cases" / chosen / str(spec.get("case"))
        run_dir.mkdir(parents=True, exist_ok=True)
        case = asyncio.create_task(
            CaseRun(spec, harness=chosen, on_event=on_event).run(run_dir)
        )
        beats = 0
        while not case.done() or not milestones.empty():
            try:
                alias, milestone = await asyncio.wait_for(
                    milestones.get(), timeout=KEEPALIVE_S
                )
            except TimeoutError:
                beats += 1
                yield event(
                    actions=EventActions(
                        state_delta={"system2_keepalive": beats}
                    )
                )
                continue
            text = f"[{alias} {milestone.kind.value}] {milestone.message}"
            yield event(
                content=types.Content(
                    role="model", parts=[types.Part(text=text)]
                )
            )
        report = json.loads(json.dumps(await case, default=str))
        (run_dir / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        response = types.FunctionResponse(name=TOOL_NAME, response=report)
        yield event(
            content=types.Content(
                role="user", parts=[types.Part(function_response=response)]
            )
        )
        yield event(
            content=types.Content(
                role="model", parts=[types.Part(text=final_text(report))]
            )
        )


root_agent = System2CaseAgent(
    name="system2_agent",
    description="Runs one System 2 case on a docker computer through the real thread registry.",
)

app = App(root_agent=root_agent, name="system2_agent")
