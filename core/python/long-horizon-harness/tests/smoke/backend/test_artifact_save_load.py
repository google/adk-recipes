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

"""Lock in the artifact save → display_name → load round-trip.

Regression net for the bug where ``Part.from_bytes()`` was used in
``app/tools/artifacts.py`` without setting ``Blob.display_name`` — the
ADK genai→A2A converter then emitted ``FilePart.name=None`` and the UI
fell back to ``attachment.<ext>``, breaking generated-file downloads.

Asserts on three observable side effects, never on LLM text:
1. ``save`` reports success and the saved-under filename.
2. The saved ``Part``'s ``inline_data.display_name`` matches the
   filename — this is the field that flows into ``FilePart.name``.
3. ``load`` recovers the bytes intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.adk.environment import BaseEnvironment

from tests.smoke.backend.conftest import _FakeToolContext

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_save_then_load_round_trips_payload_and_filename(
    active_env: BaseEnvironment,
    tool_context: _FakeToolContext,
    unique_name: str,
) -> None:
    from app.tools.artifacts import artifact

    payload = b"\x89PNG\r\n\x1a\nfake-pixels-" + unique_name.encode()
    workspace_path = Path(f"{unique_name}.png")
    abs_path = active_env.working_dir / workspace_path
    await active_env.write_file(abs_path, payload)

    save_result = await artifact(
        action="save", path=str(workspace_path), tool_context=tool_context
    )
    assert save_result["success"] is True
    assert save_result["filename"] == workspace_path.name

    saved_part = await tool_context.load_artifact(filename=workspace_path.name)
    assert saved_part is not None
    assert saved_part.inline_data is not None
    assert saved_part.inline_data.display_name == workspace_path.name
    assert saved_part.inline_data.data == payload
    assert (
        tool_context.actions.artifact_delta.get(workspace_path.name) is not None
    )

    dest_path = Path(f"{unique_name}_copy.png")
    load_result = await artifact(
        action="load",
        name=workspace_path.name,
        dest_path=str(dest_path),
        tool_context=tool_context,
    )
    assert load_result["success"] is True

    abs_dest = active_env.working_dir / dest_path
    assert await active_env.read_file(abs_dest) == payload


async def test_save_missing_file_returns_error_not_exception(
    active_env: BaseEnvironment,
    tool_context: _FakeToolContext,
    unique_name: str,
) -> None:
    from app.tools.artifacts import artifact

    missing = f"{unique_name}_does_not_exist.txt"
    result = await artifact(
        action="save", path=missing, tool_context=tool_context
    )

    assert result["success"] is False
    assert "not found" in result["error"].lower()
