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

"""Unit tests for ``FilenamePreservingArtifactService``.

Locks in the wrapper that re-attaches ``Blob.display_name`` after the
inner service strips it on ``load_artifact``. Covers three shapes:

1. Inner strips ``display_name`` (FileArtifactService / GcsArtifactService
   behavior) → wrapper restores it from ``filename``.
2. Inner preserves ``display_name`` (InMemoryArtifactService) → wrapper
   passes through untouched, never overwriting an intentional name.
3. Inner returns ``None`` (artifact missing) → wrapper returns ``None``
   without dereferencing.
"""

from __future__ import annotations

import pytest
from google.adk.artifacts.in_memory_artifact_service import (
    InMemoryArtifactService,
)
from google.genai import types

from app.infrastructure.artifact_service import (
    FilenamePreservingArtifactService,
)


class _StripsDisplayName(InMemoryArtifactService):
    """Mimics FileArtifactService / GcsArtifactService: returns a Part
    rebuilt from raw bytes + mime_type, with display_name dropped."""

    async def load_artifact(self, **kwargs):  # type: ignore[override]
        part = await super().load_artifact(**kwargs)
        if part is None or part.inline_data is None:
            return part
        return types.Part(
            inline_data=types.Blob(
                mime_type=part.inline_data.mime_type,
                data=part.inline_data.data,
            )
        )


@pytest.mark.asyncio
async def test_load_restores_display_name_when_inner_strips_it() -> None:
    inner = _StripsDisplayName()
    wrapped = FilenamePreservingArtifactService(inner)
    await wrapped.save_artifact(
        app_name="app",
        user_id="u",
        session_id="s",
        filename="report.png",
        artifact=types.Part(
            inline_data=types.Blob(
                mime_type="image/png", data=b"pixels", display_name="report.png"
            )
        ),
    )

    part = await wrapped.load_artifact(
        app_name="app", user_id="u", session_id="s", filename="report.png"
    )

    assert part is not None
    assert part.inline_data is not None
    assert part.inline_data.display_name == "report.png"
    assert part.inline_data.data == b"pixels"
    assert part.inline_data.mime_type == "image/png"


@pytest.mark.asyncio
async def test_load_passes_through_when_inner_preserves_display_name() -> None:
    """If the inner service already set display_name, don't overwrite it —
    the inner may have intentionally set something other than the filename
    (e.g. a friendly name in custom_metadata)."""
    inner = InMemoryArtifactService()
    wrapped = FilenamePreservingArtifactService(inner)
    await wrapped.save_artifact(
        app_name="app",
        user_id="u",
        session_id="s",
        filename="stored_as.png",
        artifact=types.Part(
            inline_data=types.Blob(
                mime_type="image/png",
                data=b"pixels",
                display_name="friendly.png",
            )
        ),
    )

    part = await wrapped.load_artifact(
        app_name="app", user_id="u", session_id="s", filename="stored_as.png"
    )

    assert part is not None
    assert part.inline_data is not None
    assert part.inline_data.display_name == "friendly.png"


@pytest.mark.asyncio
async def test_load_returns_none_when_inner_returns_none() -> None:
    inner = InMemoryArtifactService()
    wrapped = FilenamePreservingArtifactService(inner)

    part = await wrapped.load_artifact(
        app_name="app", user_id="u", session_id="s", filename="missing.png"
    )

    assert part is None
