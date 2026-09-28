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

"""``artifact(action='save'|'load')`` meta-tool dispatch.

Bridges the per-user workspace and ADK's ``ArtifactService``. A single
dispatch lets the agent either push a finished workspace file out to the
configured artifact service, or pull a previously-saved artifact back
into the workspace so a follow-up turn can edit it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from google.genai import types as genai_types

from app.environment import LocalEnvironment


class _FakeActions:
    """Mirror of ``EventActions.artifact_delta`` — the include-artifacts
    interceptor reads this map to know which artifacts to surface."""

    def __init__(self) -> None:
        self.artifact_delta: dict[str, int] = {}


class _FakeSession:
    """The session coordinates ``_save`` reads to build the artifact link."""

    app_name = "horizon"
    user_id = "u-1"
    id = "sess-1"


class _FakeToolContext:
    """Minimal stand-in for ``ToolContext`` covering only the artifact API
    surface — backed by an in-memory ``{filename: list[Part]}`` map so
    ``save_artifact`` returns a version and ``load_artifact`` round-trips
    the saved Part."""

    def __init__(self) -> None:
        self._store: dict[str, list[genai_types.Part]] = {}
        self.actions = _FakeActions()
        self.session = _FakeSession()

    async def save_artifact(
        self,
        filename: str,
        artifact: genai_types.Part,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        versions = self._store.setdefault(filename, [])
        versions.append(artifact)
        return len(versions) - 1

    async def load_artifact(
        self, filename: str, version: int | None = None
    ) -> genai_types.Part | None:
        versions = self._store.get(filename)
        if not versions:
            return None
        if version is None:
            return versions[-1]
        return versions[version]

    async def list_artifacts(self) -> list[str]:
        return list(self._store.keys())


@pytest.fixture
def fake_ctx() -> _FakeToolContext:
    return _FakeToolContext()


@pytest.fixture
def local_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalEnvironment:
    """Wire a LocalEnvironment so the dispatch tool has real bytes to
    read/write through the env contract."""
    from app.environment_context import set_active_environment

    env = LocalEnvironment(working_dir=tmp_path)
    set_active_environment(env)
    return env


pytestmark = pytest.mark.asyncio


# =============================================================================
# Module surface
# =============================================================================


async def test_module_exposes_dispatch_entrypoint() -> None:
    from app.tools import artifacts

    assert callable(artifacts.artifact)


# =============================================================================
# action='save' — workspace -> artifact service
# =============================================================================


async def test_save_reads_from_env_and_stores_in_context(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """Happy path: file exists in the workspace, save copies the bytes
    into the artifact service and returns the version."""
    from app.tools.artifacts import artifact

    payload = b"col1,col2\n1,2\n3,4\n"
    (local_env.working_dir / "report.csv").write_bytes(payload)

    result = await artifact(
        action="save", path="report.csv", tool_context=fake_ctx
    )

    assert result["success"] is True
    assert result["filename"] == "report.csv"
    assert result["version"] == 0

    saved = await fake_ctx.load_artifact("report.csv")
    assert saved is not None
    assert saved.inline_data is not None
    assert saved.inline_data.data == payload
    assert saved.inline_data.mime_type == "text/csv"


async def test_save_strips_directory_prefix(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """The artifact name is just the basename — the workspace path is
    internal detail. ``reports/Q3/summary.csv`` becomes ``summary.csv``."""
    from app.tools.artifacts import artifact

    nested = local_env.working_dir / "reports" / "Q3"
    nested.mkdir(parents=True)
    (nested / "summary.csv").write_bytes(b"hi")

    result = await artifact(
        action="save",
        path="reports/Q3/summary.csv",
        tool_context=fake_ctx,
    )

    assert result["filename"] == "summary.csv"
    assert await fake_ctx.load_artifact("summary.csv") is not None


async def test_save_handles_binary_payload(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """Binary bytes must survive the round-trip — no UTF-8 decode pass."""
    from app.tools.artifacts import artifact

    payload = bytes([0x00, 0xFF, 0x80, 0x01])
    (local_env.working_dir / "blob.bin").write_bytes(payload)

    await artifact(action="save", path="blob.bin", tool_context=fake_ctx)

    saved = await fake_ctx.load_artifact("blob.bin")
    assert saved is not None
    assert saved.inline_data is not None
    assert saved.inline_data.data == payload
    assert saved.inline_data.mime_type == "application/octet-stream"


async def test_save_missing_file_returns_structured_error(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """The tool returns a structured error rather than raising — callers
    in the agent loop are not equipped to handle exceptions, and a clear
    error message lets the LLM choose to recover."""
    from app.tools.artifacts import artifact

    result = await artifact(
        action="save", path="does_not_exist.txt", tool_context=fake_ctx
    )

    assert result["success"] is False
    assert "does_not_exist.txt" in result["error"]
    assert await fake_ctx.list_artifacts() == []


async def test_save_records_artifact_delta_for_a2a_emission(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """The A2A artifact interceptor only emits FileParts to the client when
    ``actions.artifact_delta`` is populated. Without this, generated files
    persist in the artifact service but never reach the UI."""
    from app.tools.artifacts import artifact

    (local_env.working_dir / "chart.png").write_bytes(b"\x89PNG\r\nfake")
    await artifact(action="save", path="chart.png", tool_context=fake_ctx)

    assert fake_ctx.actions.artifact_delta == {"chart.png": 0}


async def test_save_mime_from_bytes_not_extension(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """A file saved as .png whose bytes are JPEG must carry image/jpeg, so the
    FilePart re-sent to the model on a later turn isn't rejected for a
    media-type mismatch."""
    from app.tools.artifacts import artifact

    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
    (local_env.working_dir / "out.png").write_bytes(jpeg)
    await artifact(action="save", path="out.png", tool_context=fake_ctx)

    saved = await fake_ctx.load_artifact("out.png")
    assert saved is not None
    assert saved.inline_data is not None
    assert saved.inline_data.mime_type == "image/jpeg"


async def test_save_sets_display_name_for_a2a_filepart_name(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """ADK's genai→A2A part converter copies the Blob's ``display_name`` into
    the FilePart's ``name`` field. Without it, the UI receives ``name=None``
    and falls back to a generic "attachment.png", breaking downloads of
    generated files like "damped_oscillator.png"."""
    from app.tools.artifacts import artifact

    (local_env.working_dir / "damped_oscillator.png").write_bytes(
        b"\x89PNG\r\nfake"
    )
    await artifact(
        action="save", path="damped_oscillator.png", tool_context=fake_ctx
    )

    saved = await fake_ctx.load_artifact("damped_oscillator.png")
    assert saved is not None
    assert saved.inline_data is not None
    assert saved.inline_data.display_name == "damped_oscillator.png"


async def test_save_returns_increasing_version_on_resave(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """Re-saving the same filename returns a higher version — the artifact
    service handles versioning, and the tool surfaces what version was
    assigned so the agent can disambiguate later."""
    from app.tools.artifacts import artifact

    (local_env.working_dir / "report.csv").write_bytes(b"v1")
    r1 = await artifact(action="save", path="report.csv", tool_context=fake_ctx)

    (local_env.working_dir / "report.csv").write_bytes(b"v2")
    r2 = await artifact(action="save", path="report.csv", tool_context=fake_ctx)

    assert r1["version"] == 0
    assert r2["version"] == 1


# =============================================================================
# action='load' — artifact service -> workspace
# =============================================================================


async def test_load_writes_to_env(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    """Symmetric to save: pull a stored artifact and write it into the
    workspace at the requested destination path."""
    from app.tools.artifacts import artifact

    payload = b"loaded-bytes-here"
    part = genai_types.Part.from_bytes(data=payload, mime_type="text/plain")
    await fake_ctx.save_artifact("hello.txt", artifact=part)

    result = await artifact(
        action="load",
        name="hello.txt",
        dest_path="incoming/hello.txt",
        tool_context=fake_ctx,
    )

    assert result["success"] is True
    assert (
        local_env.working_dir / "incoming" / "hello.txt"
    ).read_bytes() == payload


async def test_load_missing_name_returns_structured_error(
    local_env: LocalEnvironment, fake_ctx: _FakeToolContext
) -> None:
    from app.tools.artifacts import artifact

    result = await artifact(
        action="load",
        name="missing.txt",
        dest_path="dest.txt",
        tool_context=fake_ctx,
    )

    assert result["success"] is False
    assert "missing.txt" in result["error"]


# =============================================================================
# Dispatch error branches
# =============================================================================


class TestDispatchErrors:
    async def test_save_missing_path_rejected(
        self, local_env: LocalEnvironment, fake_ctx: _FakeToolContext
    ) -> None:
        from app.tools.artifacts import artifact

        result = await artifact(action="save", tool_context=fake_ctx)
        assert result["success"] is False
        assert "path" in result["error"]

    async def test_load_missing_name_rejected(
        self, local_env: LocalEnvironment, fake_ctx: _FakeToolContext
    ) -> None:
        from app.tools.artifacts import artifact

        result = await artifact(
            action="load", dest_path="dest.txt", tool_context=fake_ctx
        )
        assert result["success"] is False
        assert "name" in result["error"]

    async def test_load_missing_dest_path_rejected(
        self, local_env: LocalEnvironment, fake_ctx: _FakeToolContext
    ) -> None:
        from app.tools.artifacts import artifact

        result = await artifact(
            action="load", name="foo.txt", tool_context=fake_ctx
        )
        assert result["success"] is False
        assert "dest_path" in result["error"]

    async def test_unknown_action_rejected(
        self, local_env: LocalEnvironment, fake_ctx: _FakeToolContext
    ) -> None:
        from app.tools.artifacts import artifact

        result = await artifact(
            action="banana",  # type: ignore[arg-type]
            tool_context=fake_ctx,
        )
        assert result["success"] is False
        assert "action" in result["error"].lower()


# Suppress event-loop warnings introduced by transitive imports.
@pytest.fixture(autouse=True)
def _silence_loop_warning() -> None:
    asyncio.get_event_loop_policy()
