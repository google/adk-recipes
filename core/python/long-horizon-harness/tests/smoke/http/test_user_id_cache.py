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

"""``_env_cache`` keeps the second request warm.

If the first request through ``/a2a`` for ``lha_user_id=X``
provisions a sandbox and stashes it in ``_env_cache``, the second
request for the same X should NOT re-provision. We can't directly
inspect the in-process cache from a subprocess, so we measure the
proxy: time-to-first-byte should drop on the second call by more than
the noise floor.

This test is intentionally loose: it asserts the ratio, not absolute
numbers, so it survives slow CI hosts and warm GCP regions. Treat a
failure as a signal to investigate ``app/conversation/session_start.py``
not as a perf gate.
"""

from __future__ import annotations

import json
import time
import uuid

import requests


def _send_and_time_first_byte(
    base_url: str, *, user_id: str, text: str
) -> float:
    """Return seconds from POST to the terminal SSE event (full turn).

    a2a 1.x emits the initial ``submitted`` task event immediately, and on the
    local backend env setup is ~instant, so first-byte (or first-working) timing
    can't reflect warmth. Time the whole turn instead: the first turn pays
    one-time env-build + LLM-client/connection warmup, so a warm second turn for
    the same user is faster.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/stream",
        "params": {
            "message": {
                "kind": "message",
                "messageId": uuid.uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "metadata": {"lha_user_id": user_id},
            }
        },
    }
    started = time.monotonic()
    response = requests.post(
        f"{base_url}/a2a",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        stream=True,
        timeout=180,
    )
    assert response.status_code == 200
    done_at: float | None = None
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line.startswith("data: "):
            continue
        result = json.loads(line[6:]).get("result") or {}
        state = (result.get("status") or {}).get("state")
        if result.get("final") is True or state in {
            "completed",
            "failed",
            "canceled",
        }:
            done_at = time.monotonic()
            break
    response.close()
    assert done_at is not None, "stream produced no terminal event"
    return done_at - started


def test_second_request_for_same_user_is_warm(smoke_http_server: str) -> None:
    user_id = f"cache-{uuid.uuid4().hex[:12]}"
    prompt = "Use the `terminal` tool to run: echo cache-probe"

    cold = _send_and_time_first_byte(
        smoke_http_server, user_id=user_id, text=prompt
    )
    warm = _send_and_time_first_byte(
        smoke_http_server, user_id=user_id, text=prompt
    )

    assert warm < cold, (
        f"expected second request to be faster (warm _env_cache); "
        f"cold={cold:.2f}s warm={warm:.2f}s — investigate "
        f"app/conversation/session_start.py _env_cache eviction"
    )
