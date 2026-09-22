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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Run the recipe end to end and print what happened.

    python demo.py           # offline, scripted model, no API key
    python demo.py --live    # real model; needs MODEL_NAME + credentials

Offline is the default and is what the tests exercise. The ADK `Runner`,
its flows, its callbacks and its plugin manager are all the real ones —
only the model is replaced, by a `BaseLlm` subclass that replays a fixed
list of function calls. That keeps the run deterministic, free, and
runnable in CI.

Exit code 0 if every expectation held, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from attenu_guard import AuditLog, evidence
from attenu_guard.cli import main as attenu_guard_cli
from attenu_guard.wire import Ed25519Signer
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import (
    InMemorySessionService,
)
from google.genai import types
from pydantic import Field

from app import tools
from app.agent import build_app
from app.permissions import COORDINATOR, GREEDY_REQUEST

# ADK stamps the calling agent's name into `llm_request.config.labels`
# (google/adk/flows/llm_flows/base_llm_flow.py), so one model instance can
# drive a whole multi-agent scenario.
_AGENT_LABEL = "adk_agent_name"

QUESTION = "Order ORD-8812 arrived damaged. Please refund it."


def _fc(name: str, **args: Any) -> types.Part:
    return types.Part.from_function_call(name=name, args=args)


def _text(body: str) -> types.Part:
    return types.Part.from_text(text=body)


class ScriptedLlm(BaseLlm):
    """A `BaseLlm` that replays a per-agent queue of `types.Part`s."""

    model: str = "scripted-offline-model"
    # A pydantic field on ADK's BaseLlm, not a plain attribute: a
    # ClassVar here would stop `ScriptedLlm(script=...)` binding at all.
    script: dict[str, list] = Field(default_factory=dict)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        config = llm_request.config
        labels = (config.labels or {}) if config else {}
        agent = labels.get(_AGENT_LABEL)
        queue = self.script.get(agent) or []
        part = queue.pop(0) if queue else _text(f"[{agent}] nothing further.")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def script() -> dict[str, list]:
    """The coordinator looks the order up and hands billing over. The
    billing agent reads the invoice, then tries the refund it was not
    delegated."""
    return {
        "coordinator": [
            _fc("lookup_order", order_id="ORD-8812"),
            _fc("transfer_to_agent", agent_name="billing_agent"),
        ],
        "billing_agent": [
            _fc("get_invoice", invoice_id="INV-4471"),
            _fc(
                "issue_refund",
                invoice_id="INV-4471",
                amount_cents=tools.INVOICE_TOTAL_CENTS,
            ),
            _text("The refund needs human approval."),
        ],
    }


async def _drive(application, message: str) -> list:
    sessions = InMemorySessionService()
    runner = Runner(app=application, session_service=sessions)
    session = await sessions.create_session(
        app_name=application.name, user_id="demo-user"
    )
    events = []
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[_text(message)]),
    ):
        events.append(event)
    return events


def _event_parts(event) -> list:
    """An event's content parts, or `[]` for an event with no content —
    not every ADK runner event carries one (e.g. a turn-boundary event)."""
    content = getattr(event, "content", None)
    return content.parts if content and content.parts else []


def tool_responses(events) -> dict:
    """{tool_name: response} for every function response in the run."""
    out: dict[str, Any] = {}
    for event in events:
        parts = _event_parts(event)
        for part in parts:
            if part.function_response:
                out[part.function_response.name] = (
                    part.function_response.response
                )
    return out


def run(model: Any, *, audit_path: str | None = None):
    """One turn. Returns (events, root_guard, plugin)."""
    tools.reset()
    application, root_guard, plugin = build_app(model, audit_path=audit_path)
    events = asyncio.run(_drive(application, QUESTION))
    return events, root_guard, plugin


def run_offline(*, audit_path: str | None = None):
    return run(ScriptedLlm(script=script()), audit_path=audit_path)


def _print_transcript(events) -> None:
    for event in events:
        parts = _event_parts(event)
        for part in parts:
            if part.function_call:
                args = dict(part.function_call.args or {})
                print(
                    f"    [{event.author}] calls "
                    f"{part.function_call.name}({args})"
                )
            elif part.function_response:
                response = part.function_response.response
                denied = (
                    isinstance(response, dict)
                    and response.get("error") == "authority_denied"
                )
                verdict = "DENIED" if denied else "ok    "
                print(
                    f"    [{event.author}] <- {verdict} "
                    f"{part.function_response.name}"
                )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    live = "--live" in argv

    if live:
        model = os.getenv("MODEL_NAME")
        if not model:
            print("MODEL_NAME is not set — see .env.example")
            return 1
        print(f"[live] model={model}")
    else:
        model = ScriptedLlm(script=script())
        print("[offline] scripted model, no API key needed")

    workdir = Path(tempfile.mkdtemp(prefix="attenu-guard-"))
    ledger = workdir / "ledger.jsonl"

    print("\n1. one turn, two agents")
    events, root_guard, plugin = run(model, audit_path=str(ledger))
    _print_transcript(events)

    coordinator = root_guard
    billing = plugin.guard_for("billing_agent")
    responses = tool_responses(events)
    refund = responses.get("issue_refund", {})

    print("\n2. what each agent holds")
    print(f"    coordinator : {coordinator.authority}")
    print(f"    billing     : {billing.authority}")
    print(
        "    billing is narrower than coordinator: "
        f"{billing.is_narrower_than(coordinator)}"
    )

    print("\n3. the refusal")
    print(f"    tool bodies that ran: {tools.EXECUTED}")
    print(f"    issue_refund response: {refund}")
    body_ran = any(name == "issue_refund" for name, _ in tools.EXECUTED)
    refused = refund.get("error") == "authority_denied"

    print("\n4. asking for more does not produce more")
    granted = COORDINATOR.meet(GREEDY_REQUEST)
    print(f"    requested: {GREEDY_REQUEST}")
    print(f"    granted  : {granted}")
    print(
        "    granted is narrower than coordinator: "
        f"{granted.is_narrower_than(COORDINATOR)}"
    )

    print("\n5. the ledger, checked without this process")
    entries = root_guard.audit_log().entries
    chain_ok, chain_err = AuditLog.verify(entries)
    print(f"    {len(entries)} events, hash chain: {chain_ok}")
    if not chain_ok:
        print(f"    {chain_err}")

    signer = Ed25519Signer.generate(kid="recipe-demo")
    pubkey = signer.public_bytes_raw().hex()
    bundle_path = workdir / "evidence-bundle.json"
    bundle = evidence.export_bundle(root_guard.audit_log(), signer)
    bundle_path.write_text(json.dumps(bundle, indent=2))

    print(f"    bundle: {bundle_path}")
    print("    verifying it with the packaged command:")
    print(
        f"      attenu-guard verify {bundle_path.name} --pubkey {pubkey[:16]}…"
    )
    print("    ", end="")
    # `attenu_guard_cli` returns an exit code today, but it is a CLI
    # entry point — a future version could add argument parsing that
    # exits the process directly on bad input, or a success path that
    # falls off the end and returns `None`. Handle both so the rest of
    # this script still runs and the demo doesn't fail confusingly.
    try:
        verify_rc = attenu_guard_cli(
            ["verify", str(bundle_path), "--pubkey", pubkey]
        )
    except SystemExit as exc:
        # A bare `sys.exit()` carries `code=None`, which Python itself
        # treats as success (exit status 0) — mirror that here so the
        # `ok` check below agrees with process semantics.
        verify_rc = (
            0
            if exc.code is None
            else (exc.code if isinstance(exc.code, int) else 1)
        )

    graph = evidence.delegation_graph(bundle)
    print(f"    reviewer view: {len(graph['nodes'])} nodes")

    ok = (
        refused
        and not body_ran
        and billing.is_narrower_than(coordinator)
        and granted.is_narrower_than(COORDINATOR)
        and chain_ok
        and verify_rc in (0, None)
    )
    print("\nRESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
