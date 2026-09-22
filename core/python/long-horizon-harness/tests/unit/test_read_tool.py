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

"""``ReadTool`` — the merge of ``read_file`` (text, paginated) and
``view_file`` (multimodal injection) into one ``BaseTool``.

This tool has the worst review track record in the prompt-minimalism plan:
three independent review rounds each found a way to implement this merge
that passes an obvious test while silently destroying a capability. Every
test below exists to catch one of those four failure modes:

* a FunctionTool has no ``process_llm_request`` hook, so the media Part
  would never reach the model even though ``run_async`` "succeeded".
* skipping ``super().process_llm_request()`` removes the tool from the
  model's declared tools entirely (``append_tools`` never runs).
* ``.png``/``.jpg``/``.mp3``/``.mp4`` are in ``_BINARY_EXTENSIONS`` but
  ``.pdf`` is not — running the binary-extension check before dispatch
  would refuse every image while a PDF-only test stayed green.
* ``view_file`` never ran the credential deny-list or the non-regular-file
  check; porting its skeleton verbatim turns ``as_media=True`` into an
  exfiltration primitive for ``~/.ssh/id_rsa``.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from google.adk.models import LlmRequest
from google.genai import types as genai_types

from horizon.environment import LocalEnvironment
from horizon.environment_context import set_active_environment
from horizon.tools import names


class _FakeActions:
    def __init__(self) -> None:
        self.artifact_delta: dict[str, int] = {}


class _FakeToolContext:
    """Mirrors the production ToolContext surface ReadTool needs:
    artifact store, state dict, actions.artifact_delta."""

    def __init__(self) -> None:
        self._store: dict[str, list[genai_types.Part]] = {}
        self.actions = _FakeActions()
        self.state: dict[str, Any] = {}

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
        return versions[-1] if version is None else versions[version]

    async def list_artifacts(self) -> list[str]:
        return list(self._store.keys())


@pytest.fixture
def ctx() -> _FakeToolContext:
    return _FakeToolContext()


@pytest.fixture
def local_env(tmp_path: Path) -> LocalEnvironment:
    env = LocalEnvironment(working_dir=tmp_path)
    set_active_environment(env)
    return env


pytestmark = pytest.mark.asyncio


def _tool():
    from horizon.tools.read import ReadTool

    return ReadTool()


# =============================================================================
# Registration: the tool must be advertised to the model at all.
# =============================================================================


async def test_tool_is_registered_as_names_read() -> None:
    tool = _tool()
    assert tool.name == names.READ


async def test_tool_is_advertised_to_the_model(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """BaseTool.process_llm_request IS append_tools([self])
    (base_tool.py:155-169). Skip the super() call and the model loses the
    tool entirely while every other test stays green."""
    tool = _tool()
    req = LlmRequest(model="gemini-3.7-flash")

    await tool.process_llm_request(tool_context=ctx, llm_request=req)

    declared = {
        fd.name
        for t in (req.config.tools or [])
        for fd in (t.function_declarations or [])
    }
    assert names.READ in declared


# =============================================================================
# Text branch parity with the old read_file
# =============================================================================


async def test_text_file_returns_text(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    (local_env.working_dir / "hello.txt").write_text("line1\nline2\nline3\n")

    result = await _tool().run_async(
        args={"path": "hello.txt"}, tool_context=ctx
    )

    assert result["success"] is True
    assert "line1" in result["content"]
    assert "line3" in result["content"]


async def test_offset_and_limit_paging_is_unchanged(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """read_file was offset=1, limit=500 by default. None on the merged
    tool must mean the same thing, and explicit offset must page forward."""
    lines = "\n".join(f"line_{i}" for i in range(1, 1001)) + "\n"
    (local_env.working_dir / "big.txt").write_text(lines)

    page1 = await _tool().run_async(args={"path": "big.txt"}, tool_context=ctx)
    page2 = await _tool().run_async(
        args={"path": "big.txt", "offset": 501}, tool_context=ctx
    )

    assert page1["success"] and page2["success"]
    assert "line_1" in page1["content"]
    assert "line_501" in page2["content"]
    assert (
        "line_1\n" not in page2["content"]
        and "line_1:" not in page2["content"].split("\n")[0]
    )


async def test_limit_caps_returned_lines(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    lines = "\n".join(f"l{i}" for i in range(1, 1001)) + "\n"
    (local_env.working_dir / "long.txt").write_text(lines)

    result = await _tool().run_async(
        args={"path": "long.txt", "offset": 1, "limit": 10}, tool_context=ctx
    )

    numbered = [
        ln
        for ln in result["content"].splitlines()
        if ln.strip() and ln.split(":", 1)[0].strip().isdigit()
    ]
    assert len(numbered) == 10


async def test_truncation_flag_and_trailer_survive(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    from horizon.tools.file_ops import _MAX_READ_CHARS

    target = local_env.working_dir / "huge.txt"
    line = "a" * 999 + "\n"
    target.write_text(line * 500)

    result = await _tool().run_async(
        args={"path": "huge.txt", "limit": 10_000}, tool_context=ctx
    )

    assert result["success"] is True
    assert len(result["content"]) <= _MAX_READ_CHARS
    assert result.get("truncated") is True


async def test_use_offset_trailer_present_on_partial_read(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    lines = "\n".join(f"l{i}" for i in range(1, 101)) + "\n"
    (local_env.working_dir / "partial.txt").write_text(lines)

    result = await _tool().run_async(
        args={"path": "partial.txt", "limit": 10}, tool_context=ctx
    )

    assert "Use offset=" in result["content"]


async def test_missing_file_returns_error(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    result = await _tool().run_async(
        args={"path": "nope.txt"}, tool_context=ctx
    )
    assert result["success"] is False
    assert "error" in result


async def test_path_under_root_is_still_the_first_gate(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    result = await _tool().run_async(
        args={"path": "../../../etc/passwd"}, tool_context=ctx
    )
    assert result["success"] is False
    assert ctx.actions.artifact_delta == {}


# =============================================================================
# Guard 1 (credential deny list) and guard 2 (non-regular file), both
# branches. Neither existed on view_file.run_async at all.
# =============================================================================


async def test_credential_paths_are_denied(
    local_env: LocalEnvironment,
    ctx: _FakeToolContext,
    monkeypatch,
) -> None:
    # The deny-list check runs before the media/text dispatch decision, so
    # this fires regardless of which branch auto-detect would otherwise
    # pick — no as_media override exists to route around it either way.
    # _is_write_denied resolves ~ via $HOME. Point HOME at the workspace
    # root so a workspace-relative .ssh/id_rsa lands under the denied
    # prefix, without needing the real test-runner's home directory.
    monkeypatch.setenv("HOME", str(local_env.working_dir))
    ssh_dir = local_env.working_dir / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("fake-key-material")

    result = await _tool().run_async(
        args={"path": ".ssh/id_rsa"}, tool_context=ctx
    )

    assert result["success"] is False
    assert "protected paths" in result["error"].lower()
    assert ctx.actions.artifact_delta == {}


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX FIFO not available on Windows"
)
async def test_fifo_is_rejected_on_media_path(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    fifo_path = local_env.working_dir / "myfifo"
    os.mkfifo(str(fifo_path))

    result = await _tool().run_async(args={"path": "myfifo"}, tool_context=ctx)
    assert result.get("error")


def _tmp_is_writable() -> bool:
    # AF_UNIX paths cap at ~104 chars, so this test needs /tmp rather than
    # pytest's long tmp_path. A restricted sandbox can make /tmp unwritable.
    try:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="rtprobe"):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    sys.platform == "win32", reason="AF_UNIX sockets not on Windows"
)
@pytest.mark.skipif(not _tmp_is_writable(), reason="/tmp is not writable")
async def test_unix_socket_is_rejected_on_media_path(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rt") as td:
        sock_path = Path(td) / "s"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(sock_path))
            result = await _tool().run_async(
                args={"path": str(sock_path)},
                tool_context=ctx,
            )
        finally:
            sock.close()
    assert result.get("error")


# =============================================================================
# Guard ordering: dispatch before _has_binary_extension. .png/.jpg/.mp3/.mp4
# are in _BINARY_EXTENSIONS but .pdf is not — a PDF-only test would pass
# while an ordering bug silently refused every image.
# =============================================================================


async def test_png_returns_a_media_part_by_default(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    (local_env.working_dir / "chart.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )

    result = await _tool().run_async(
        args={"path": "chart.png"}, tool_context=ctx
    )

    assert "binary" not in str(result.get("error", "")).lower()
    assert result["success"] is True

    req = LlmRequest(model="gemini-3.7-flash")
    await _tool().process_llm_request(tool_context=ctx, llm_request=req)
    assert any(
        getattr(p, "inline_data", None)
        for c in req.contents
        for p in (c.parts or [])
    )


async def test_svg_stays_text_by_default(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """image/svg+xml is under the image/ top-level type but is readable XML
    markup, not a raster image Gemini can decode. A blanket image/*
    top-level check would route it to the media branch by default and
    silently break `read("chart.svg")` for the exact format this project
    recommends for generated charts."""
    (local_env.working_dir / "chart.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    )

    result = await _tool().run_async(
        args={"path": "chart.svg"}, tool_context=ctx
    )

    assert result["success"] is True
    assert "<svg" in result["content"]


async def test_playlist_audio_mime_stays_text_by_default(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """.m3u guesses to audio/x-mpegurl, under the audio/ top-level type,
    but it is a text playlist, not decodable audio bytes."""
    (local_env.working_dir / "list.m3u").write_text("track1.mp3\ntrack2.mp3\n")

    result = await _tool().run_async(
        args={"path": "list.m3u"}, tool_context=ctx
    )

    assert result["success"] is True
    assert "track1.mp3" in result["content"]


async def test_pdf_returns_a_media_part_by_default(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """.pdf is deliberately absent from _BINARY_EXTENSIONS (search_files
    reuses that set to skip binaries during text search), so auto-detect
    must route it to media by guessed MIME type, not by that set."""
    (local_env.working_dir / "doc.pdf").write_bytes(
        b"%PDF-1.4\nfake-pdf-bytes\n%%EOF"
    )

    result = await _tool().run_async(args={"path": "doc.pdf"}, tool_context=ctx)

    assert result["success"] is True
    assert result["mime_type"] == "application/pdf"


async def test_media_part_is_injected_into_next_turn(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    payload = b"%PDF-1.4\nfake-pdf-bytes\n%%EOF"
    (local_env.working_dir / "doc.pdf").write_bytes(payload)

    tool = _tool()
    await tool.run_async(args={"path": "doc.pdf"}, tool_context=ctx)

    req = LlmRequest(model="gemini-3.7-flash")
    await tool.process_llm_request(tool_context=ctx, llm_request=req)

    parts = [p for c in req.contents for p in (c.parts or [])]
    assert any(
        getattr(p, "inline_data", None) or getattr(p, "file_data", None)
        for p in parts
    )


async def test_artifact_delta_is_populated(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    (local_env.working_dir / "doc.pdf").write_bytes(b"%PDF-1.4 x\n%%EOF")

    await _tool().run_async(args={"path": "doc.pdf"}, tool_context=ctx)

    assert ctx.actions.artifact_delta  # drives the A2A FilePart in chat


async def test_pending_list_is_cleared_after_injection(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    (local_env.working_dir / "doc.pdf").write_bytes(b"%PDF-1.4 x\n%%EOF")

    tool = _tool()
    await tool.run_async(args={"path": "doc.pdf"}, tool_context=ctx)

    req1, req2 = (
        LlmRequest(model="gemini-3.7-flash"),
        LlmRequest(model="gemini-3.7-flash"),
    )
    await tool.process_llm_request(tool_context=ctx, llm_request=req1)
    before = len(req2.contents)
    await tool.process_llm_request(tool_context=ctx, llm_request=req2)
    assert len(req2.contents) == before  # never re-inject the same file


async def test_process_llm_request_noop_when_nothing_pending(
    ctx: _FakeToolContext,
) -> None:
    req = LlmRequest(model="gemini-3.7-flash")
    before = len(req.contents)

    await _tool().process_llm_request(tool_context=ctx, llm_request=req)

    assert len(req.contents) == before


async def test_process_llm_request_skips_evicted_artifacts(
    ctx: _FakeToolContext,
) -> None:
    ctx.state["_pending_media_reads"] = ["gone.pdf"]
    req = LlmRequest(model="gemini-3.7-flash")
    before = len(req.contents)

    await _tool().process_llm_request(tool_context=ctx, llm_request=req)

    assert len(req.contents) == before


# =============================================================================
# Model-capability gating (can_view_mime / max_image_bytes), ported from
# ViewFileTool's tests since the media path absorbs this behavior verbatim.
# =============================================================================


def _restricted_caps(*, max_image_bytes: int | None = None):
    from horizon.models.capabilities import ModelCapabilities

    def _image_or_pdf(mime: str | None) -> bool:
        base = (mime or "").split(";")[0].strip().lower()
        return base.startswith("image/") or base == "application/pdf"

    return ModelCapabilities(
        max_image_bytes=max_image_bytes,
        can_view_mime=_image_or_pdf,
        prepare_contents=None,
    )


async def test_unviewable_mime_not_injected_but_surfaced(
    local_env: LocalEnvironment, ctx: _FakeToolContext, monkeypatch
) -> None:
    from horizon.tools import read as read_mod

    monkeypatch.setattr(
        read_mod, "model_capabilities", lambda _n: _restricted_caps()
    )
    (local_env.working_dir / "clip.mp3").write_bytes(b"ID3\x03\x00audio")

    result = await _tool().run_async(
        args={"path": "clip.mp3"}, tool_context=ctx
    )

    assert result["success"] is True
    assert result["model_can_view"] is False
    assert ctx.actions.artifact_delta == {"clip.mp3": 0}


async def test_oversized_image_not_injected_when_capped(
    local_env: LocalEnvironment, ctx: _FakeToolContext, monkeypatch
) -> None:
    from horizon.tools import read as read_mod

    cap = 5 * 1024 * 1024
    monkeypatch.setattr(
        read_mod,
        "model_capabilities",
        lambda _n: _restricted_caps(max_image_bytes=cap),
    )
    raw_budget = cap * 3 // 4
    (local_env.working_dir / "big.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * (raw_budget + 1024)
    )

    result = await _tool().run_async(args={"path": "big.png"}, tool_context=ctx)

    assert result["success"] is True
    assert result["too_large"] is True


# =============================================================================
# Extensionless files, and forcing an unrecognized-extension file into media
# view, are ACCEPTED CAPABILITY LOSSES from dropping as_media (the minimal
# parameter space is read(path, offset, limit), no override). view_file
# used to
# treat ANY path as media unconditionally; auto-detect has no extension to
# key off an extensionless file, and there is no override left to force it.
# =============================================================================


async def test_extensionless_file_stays_text(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    (local_env.working_dir / "blob").write_bytes(b"\x00\xff\x80\x01")

    result = await _tool().run_async(args={"path": "blob"}, tool_context=ctx)

    # No recognized extension -> auto-detect defaults to the text branch,
    # which decodes the bytes with errors="replace" rather than erroring.
    assert result["success"] is True
    assert "content" in result


async def test_two_same_basename_media_files_inject_distinct_payloads(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """Reading a/logo.png then b/logo.png must inject both distinct versions."""
    sub1 = local_env.working_dir / "sub1"
    sub2 = local_env.working_dir / "sub2"
    sub1.mkdir()
    sub2.mkdir()

    png_head = b"\x89PNG\r\n\x1a\n"
    (sub1 / "logo.png").write_bytes(png_head + b"FIRST_IMAGE_PAYLOAD")
    (sub2 / "logo.png").write_bytes(png_head + b"SECOND_IMAGE_PAYLOAD")

    tool = _tool()
    res1 = await tool.run_async(
        args={"path": "sub1/logo.png"}, tool_context=ctx
    )
    res2 = await tool.run_async(
        args={"path": "sub2/logo.png"}, tool_context=ctx
    )
    assert res1["success"] is True
    assert res2["success"] is True

    req = LlmRequest(model="gemini-3.7-flash")
    await tool.process_llm_request(tool_context=ctx, llm_request=req)

    # Must have injected both distinct parts
    injected_data = [
        p.inline_data.data
        for c in req.contents
        for p in (c.parts or [])
        if getattr(p, "inline_data", None)
    ]
    assert len(injected_data) == 2
    assert b"FIRST_IMAGE_PAYLOAD" in injected_data[0]
    assert b"SECOND_IMAGE_PAYLOAD" in injected_data[1]

    # Labels must reflect workspace-relative paths
    labels = [
        p.text
        for c in req.contents
        for p in (c.parts or [])
        if getattr(p, "text", None) and "Contents of" in p.text
    ]
    assert len(labels) == 2
    assert "sub1/logo.png" in labels[0]
    assert "sub2/logo.png" in labels[1]


async def test_legacy_string_pending_entry_still_loads(
    local_env: LocalEnvironment, ctx: _FakeToolContext
) -> None:
    """Plain-string entries in _pending_media_reads must still inject."""
    from horizon.tools.read import _PENDING_STATE_KEY

    png_data = b"\x89PNG\r\n\x1a\nlegacy"
    part = genai_types.Part(
        inline_data=genai_types.Blob(
            data=png_data,
            mime_type="image/png",
            display_name="legacy.png",
        )
    )
    await ctx.save_artifact(filename="legacy.png", artifact=part)
    ctx.state[_PENDING_STATE_KEY] = ["legacy.png"]

    tool = _tool()
    req = LlmRequest(model="gemini-3.7-flash")
    await tool.process_llm_request(tool_context=ctx, llm_request=req)

    injected = [
        p.inline_data.data
        for c in req.contents
        for p in (c.parts or [])
        if getattr(p, "inline_data", None)
    ]
    assert len(injected) == 1
    assert injected[0] == png_data
