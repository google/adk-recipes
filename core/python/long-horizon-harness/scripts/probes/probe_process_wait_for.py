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

"""Live-LLM smoke for ``process(action='wait_for', pattern=...)``.

Drives the REAL root agent (real Gemini) to start a background command that
prints a readiness marker after a delay, then block on ``wait_for`` until the
marker appears — the "start a dev server, wait until it's ready" pattern without
polling.

Run:  (env from .env)  uv run python scripts/probes/probe_process_wait_for.py
Hits Vertex — needs GCP auth + GOOGLE_CLOUD_* set.
"""

from __future__ import annotations

import asyncio

from google.genai import types

_MARKER = "SERVER-READY-7F3A"
_PROMPT = (
    "Do exactly two steps and then report. (1) Call the `terminal` tool with "
    "background=True and this exact command: "
    f"sh -c 'sleep 3; echo {_MARKER}; sleep 60'. (2) Call the `process` tool with "
    f"action='wait_for', the session_id from step 1, and pattern='{_MARKER}' to "
    "block until the output contains the marker. Then tell me whether the marker "
    "appeared and kill the process."
)


def _calls(events: list) -> list[types.FunctionCall]:
    return [fc for e in events for fc in (e.get_function_calls() or [])]


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
    user_id = "probe-user"
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id
    )

    print(
        "=== ask root to start a bg process and wait_for a readiness marker ==="
    )
    events = [
        e
        async for e in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=_PROMPT)]
            ),
        )
    ]

    calls = _calls(events)
    names = [c.name for c in calls]
    print("tool calls seen:", names)
    wait_for = [
        c
        for c in calls
        if c.name == "process" and (c.args or {}).get("action") == "wait_for"
    ]
    if wait_for:
        print("wait_for call args:", dict(wait_for[0].args or {}))

    final = _text(events)
    print("\nfinal reply:\n", final[:1500])

    used_wait_for = bool(wait_for)
    marker_seen = (
        _MARKER in final
        or "ready" in final.lower()
        or "appeared" in final.lower()
    )
    ok = "terminal" in names and used_wait_for and marker_seen
    print(
        f"\nRESULT: {'PASS' if ok else 'INCONCLUSIVE'} "
        f"(terminal={'terminal' in names}, wait_for={used_wait_for}, "
        f"marker_reported={marker_seen})"
    )


if __name__ == "__main__":
    asyncio.run(main())
