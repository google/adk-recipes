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

"""Live-LLM smoke for the child ``ask_parent`` escalation channel.

Drives the REAL Horizon runner (real root + child Gemini models) so a delegated
child hits a decision it cannot make, calls ``ask_parent``, the question surfaces
to the "user" as an ``adk_request_confirmation``, we answer with FREE TEXT, and
the child resumes in place and reports the answer.

Run:  (env from .env)  uv run python scripts/probes/probe_ask_parent.py
Hits Vertex — needs GCP auth + GOOGLE_CLOUD_* set.
"""

from __future__ import annotations

import asyncio

from google.genai import types

_CONFIRM_FN = "adk_request_confirmation"
_ANSWER = "us-central1"
_PROMPT = (
    "Use the `delegate` tool to hand a sub-agent EXACTLY this task, and give it "
    "no toolsets and no tools: 'You must state which GCP region to deploy to, but "
    "the region was not provided and you cannot guess it. Call the `ask_parent` "
    "tool to ask the parent which region to use, then report back the exact region "
    "you were told.' Then relay the sub-agent's answer."
)


def _confirm_call(events: list) -> types.FunctionCall | None:
    for e in events:
        for fc in e.get_function_calls() or []:
            if fc.name == _CONFIRM_FN:
                return fc
    return None


def _tool_names(events: list) -> list[str]:
    return [fc.name for e in events for fc in (e.get_function_calls() or [])]


def _text(events: list) -> str:
    chunks = []
    for e in events:
        for p in getattr(getattr(e, "content", None), "parts", None) or []:
            if getattr(p, "text", None):
                chunks.append(p.text)
    return "".join(chunks)


async def main() -> None:
    from app.fast_api_app import build_runner

    runner = build_runner()
    app_name = runner.app_name
    user_id = "probe-user"
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id
    )

    print("=== turn 1: ask root to delegate a task the child must escalate ===")
    run1 = [
        e
        async for e in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=_PROMPT)]
            ),
        )
    ]
    print("tool calls seen:", _tool_names(run1))
    conf = _confirm_call(run1)
    if conf is None:
        print(
            "NO ask_parent escalation surfaced. Text so far:\n",
            _text(run1)[:1500],
        )
        print("\nRESULT: INCONCLUSIVE (model did not trigger ask_parent).")
        return

    tc = (conf.args or {}).get("toolConfirmation", {})
    print(f"\n*** child escalated to parent: id={conf.id}")
    print("    question:", tc.get("hint"))

    print(
        f"\n=== turn 2: parent answers FREE TEXT ({_ANSWER!r}) -> child resumes ==="
    )
    answer = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=conf.id,
                    name=_CONFIRM_FN,
                    response={
                        "confirmed": True,
                        "payload": {"answer": _ANSWER},
                    },
                )
            )
        ],
    )
    run2 = [
        e
        async for e in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=answer
        )
    ]
    final = _text(run2)
    print("final reply:\n", final[:2000])
    ok = _ANSWER in final
    print(
        f"\nRESULT: {'PASS' if ok else 'INCONCLUSIVE'} "
        f"(free-text answer {'reached the child and came back' if ok else 'not found'})"
    )


if __name__ == "__main__":
    asyncio.run(main())
