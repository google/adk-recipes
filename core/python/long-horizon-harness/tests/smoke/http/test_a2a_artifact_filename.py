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

"""End-to-end regression net for the artifact filename bug.

The bug was: ``app/tools/artifacts.py`` saved ``Part.from_bytes()``
without setting ``Blob.display_name``; the ADK genai→A2A converter
then emitted ``FilePart.name=None`` and the UI fell back to
``attachment.<ext>``. The Phase 1 tool-level test
(``tests/smoke/backend/test_artifact_save_load.py``) locks in the
``inline_data.display_name`` assertion. THIS test asserts the same
invariant survives the full FastAPI → A2A → genai-converter chain —
the layer where the bug actually manifested to the user.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import requests


def _stream_a2a(
    base_url: str, *, user_id: str, text: str, timeout: int = 180
) -> list[dict[str, Any]]:
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
    response = requests.post(
        f"{base_url}/a2a",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        stream=True,
        timeout=timeout,
    )
    assert response.status_code == 200, (
        f"/a2a returned {response.status_code}: {response.text[:500]}"
    )
    events: list[dict[str, Any]] = []
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _collect_file_part_names(events: list[dict[str, Any]]) -> list[str | None]:
    """Walk every ``parts`` array in the stream and pull the ``name`` off
    each ``FilePart``-shaped entry (``kind == "file"``)."""
    names: list[str | None] = []
    for event in events:
        result = event.get("result") or {}
        for parts_container in (
            result,
            result.get("status", {}).get("message", {}) or {},
            result.get("artifact", {}) or {},
            result.get("message", {}) or {},
        ):
            for part in parts_container.get("parts", []) or []:
                if part.get("kind") == "file":
                    file_field = part.get("file") or {}
                    names.append(file_field.get("name"))
    return names


def test_a2a_artifact_save_emits_filepart_with_real_name(
    smoke_http_server: str, smoke_http_user_id: str, unique_marker: str
) -> None:
    filename = f"smoke_{unique_marker}.txt"
    events = _stream_a2a(
        smoke_http_server,
        user_id=smoke_http_user_id,
        text=(
            f"First use the `terminal` tool to run: echo hello-{unique_marker} > {filename}. "
            f"Then use the `artifact` tool with action=save and path={filename} "
            "to save it as a downloadable artifact for the user."
        ),
    )

    names = _collect_file_part_names(events)
    assert names, (
        "expected at least one FilePart in the A2A stream — got none; "
        "the agent likely didn't call the `artifact` tool"
    )

    assert filename in names, (
        f"FilePart.name regression: expected {filename!r} in stream, "
        f"got names={names!r}. If any are None, the "
        f"`Blob.display_name` fix in app/tools/artifacts.py has regressed."
    )
    assert None not in names, (
        f"FilePart.name regression: stream contained a FilePart with name=None; "
        f"all names: {names!r}"
    )
