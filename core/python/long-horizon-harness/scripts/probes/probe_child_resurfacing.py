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

"""Live-LLM smoke for child permission resurfacing.

Drives the REAL Horizon runner (real root + child Gemini models) so a delegated
child hits a gated `terminal` op, the approval surfaces to the "user" as an
`adk_request_confirmation`, we approve, and the child resumes to completion.

Run:  (env from .env)  uv run python scripts/probes/probe_child_resurfacing.py
Hits Vertex — needs GCP auth + GOOGLE_CLOUD_* set.
"""

from __future__ import annotations

import asyncio

from google.genai import types

_CONFIRM_FN = "adk_request_confirmation"
_PROMPT = (
    "Use the `delegate` tool (do NOT run the terminal yourself) to delegate this "
    "to a sub-agent: instruct the sub-agent to run the shell command "
    "`printf 'hello-from-child\\n'` with its terminal tool and report the exact "
    "output. Give the sub-agent the 'shell' toolset."
)


def _confirm_call(events: list) -> types.FunctionCall | None:
    for e in events:
        for fc in e.get_function_calls() or []:
            if fc.name == _CONFIRM_FN:
                return fc
    return None


def _tool_names(events: list) -> list[str]:
    out: list[str] = []
    for e in events:
        for fc in e.get_function_calls() or []:
            out.append(fc.name)
    return out


def _text(events: list) -> str:
    chunks = []
    for e in events:
        content = getattr(e, "content", None)
        for p in getattr(content, "parts", None) or []:
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

    print("=== turn 1: ask root to delegate a gated terminal op ===")
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
        print("NO confirmation surfaced. Text so far:\n", _text(run1)[:1500])
        print(
            "\nRESULT: could not trigger a child approval (model chose differently)."
        )
        return

    print(f"\n*** child approval surfaced to user: id={conf.id}")
    tc = (conf.args or {}).get("toolConfirmation", {})
    print("    hint:", tc.get("hint"))

    print("\n=== turn 2: user APPROVES -> child resumes in place ===")
    approve = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=conf.id, name=_CONFIRM_FN, response={"confirmed": True}
                )
            )
        ],
    )
    run2 = [
        e
        async for e in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=approve
        )
    ]
    final = _text(run2)
    print("final reply:\n", final[:2000])
    ok = "hello-from-child" in final
    print(
        f"\nRESULT: {'PASS' if ok else 'INCONCLUSIVE'} "
        f"(child output {'echoed back' if ok else 'not found in reply'})"
    )


if __name__ == "__main__":
    asyncio.run(main())
