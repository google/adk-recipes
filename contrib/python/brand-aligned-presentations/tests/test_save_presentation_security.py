# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression tests for save_presentation security boundaries (b/565095907)."""

import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from app.tools.artifact_utils import save_presentation

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.store = {}
    context.state = {}

    async def mock_save(name, artifact):
        if isinstance(artifact, types.Part):
            data = artifact.inline_data.data
        else:
            data = artifact
        context.store[name] = data
        return name

    context.save_artifact = AsyncMock(side_effect=mock_save)
    return context


async def test_save_presentation_rejects_arbitrary_system_files(mock_context):
    """Ensure save_presentation rejects arbitrary files like /etc/passwd."""
    # /etc/passwd exists on Linux systems
    passwd_path = "/etc/passwd"
    assert Path(passwd_path).exists()

    result = await save_presentation(mock_context, "exfil.pptx", passwd_path)

    assert result.startswith("Error:")
    assert "exfil.pptx" not in mock_context.store


async def test_save_presentation_rejects_non_pptx_files_in_temp(mock_context):
    """Ensure non-pptx files in temp dir are rejected even if named .pptx."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"not a presentation")
        fake_txt = f.name

    try:
        result = await save_presentation(mock_context, "deck.pptx", fake_txt)
        assert result.startswith("Error:")
        assert "deck.pptx" not in mock_context.store
    finally:
        Path(fake_txt).unlink(missing_ok=True)


async def test_save_presentation_accepts_valid_pptx(mock_context):
    """Ensure valid PPTX file in tempdir is accepted and saved."""
    with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
        # Create a minimal valid zip file representing pptx
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("[Content_Types].xml", b"<Types/>")
        valid_pptx = f.name

    try:
        result = await save_presentation(mock_context, "output.pptx", valid_pptx)
        assert result.startswith("Successfully saved the presentation as artifact")
        assert "output.pptx" in mock_context.store
    finally:
        Path(valid_pptx).unlink(missing_ok=True)
