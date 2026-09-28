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

"""Wrapper that re-attaches ``Blob.display_name`` after ``load_artifact``.

ADK's ``FileArtifactService`` and ``GcsArtifactService`` reconstruct
``Part(inline_data=Blob(mime_type, data))`` on load without restoring
``display_name``. That field flows into the A2A ``FilePart.name`` over the
genai→A2A converter; when it's ``None`` the UI falls back to
``attachment.<ext>``. This wrapper restores it from the ``filename``
argument so the UI shows the real name.

Drop when both upstream services preserve ``display_name`` on load.
"""

from __future__ import annotations

from typing import Any

from google.adk.artifacts.base_artifact_service import (
    ArtifactVersion,
    BaseArtifactService,
)
from google.genai import types


class FilenamePreservingArtifactService(BaseArtifactService):
    """Delegates everything to ``inner`` but re-attaches the filename as
    ``Blob.display_name`` when the inner service strips it on load."""

    def __init__(self, inner: BaseArtifactService) -> None:
        self._inner = inner

    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: types.Part | dict[str, Any],
        session_id: str | None = None,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        return await self._inner.save_artifact(
            app_name=app_name,
            user_id=user_id,
            filename=filename,
            artifact=artifact,
            session_id=session_id,
            custom_metadata=custom_metadata,
        )

    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
        version: int | None = None,
    ) -> types.Part | None:
        part = await self._inner.load_artifact(
            app_name=app_name,
            user_id=user_id,
            filename=filename,
            session_id=session_id,
            version=version,
        )
        if (
            part is None
            or part.inline_data is None
            or part.inline_data.display_name
        ):
            return part
        return types.Part(
            inline_data=types.Blob(
                mime_type=part.inline_data.mime_type,
                data=part.inline_data.data,
                display_name=filename,
            )
        )

    async def list_artifact_keys(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str | None = None,
    ) -> list[str]:
        return await self._inner.list_artifact_keys(
            app_name=app_name, user_id=user_id, session_id=session_id
        )

    async def delete_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
    ) -> None:
        return await self._inner.delete_artifact(
            app_name=app_name,
            user_id=user_id,
            filename=filename,
            session_id=session_id,
        )

    async def list_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
    ) -> list[int]:
        return await self._inner.list_versions(
            app_name=app_name,
            user_id=user_id,
            filename=filename,
            session_id=session_id,
        )

    async def list_artifact_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
    ) -> list[ArtifactVersion]:
        return await self._inner.list_artifact_versions(
            app_name=app_name,
            user_id=user_id,
            filename=filename,
            session_id=session_id,
        )

    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: str | None = None,
        version: int | None = None,
    ) -> ArtifactVersion | None:
        return await self._inner.get_artifact_version(
            app_name=app_name,
            user_id=user_id,
            filename=filename,
            session_id=session_id,
            version=version,
        )


__all__ = ["FilenamePreservingArtifactService"]
