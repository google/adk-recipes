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


from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from presentation_agent.tools.artifact_utils import (
    save_deck_spec,
    update_slide_in_spec,
)

# Use pytest-asyncio for async tests
pytestmark = pytest.mark.asyncio


# ==============================================================================
# Setup: Mock ToolContext for persistent artifacts
# ==============================================================================
@pytest.fixture
def mock_context():
    context = MagicMock()
    # Mock storage dictionary
    context.store = {}
    context.state = {}

    async def mock_save(name, artifact):
        # Extract bytes from the Part object
        if isinstance(artifact, types.Part):
            data = artifact.inline_data.data
        else:
            data = artifact
        context.store[name] = data
        return name

    async def mock_load(name):
        data = context.store.get(name)
        if not data:
            return None
        # Return a mocked Part object
        return types.Part(
            inline_data=types.Blob(data=data, mime_type="application/json")
        )

    context.save_artifact = AsyncMock(side_effect=mock_save)
    context.load_artifact = AsyncMock(side_effect=mock_load)
    return context


# ==============================================================================
# Tests for save_deck_spec
# ==============================================================================


async def test_save_deck_spec_success(mock_context):
    """Test successful saving of a valid DeckSpec dict, including translation."""
    input_spec = {
        "cover": {"title": "Test Cover"},
        "slide_topics": [  # Test translation from slide_topics
            {"title": "Slide 1", "layout_name": "Title and Content"}
        ],
    }

    result = await save_deck_spec(mock_context, input_spec)

    assert "Success:" in result

    # Verify it was saved to the context state
    assert "current_deck_spec" in mock_context.state

    # Verify translation worked
    saved_data = mock_context.state["current_deck_spec"]
    assert "slides" in saved_data
    assert saved_data["slides"][0]["title"] == "Slide 1"
    assert saved_data["slides"][0]["layout_name"] == "Title and Content"
    assert saved_data["closing_title"] == "Thank You"


async def test_save_deck_spec_invalid_schema(mock_context):
    """Test saving a DeckSpec with missing required fields (should fail cleanly)."""
    # Cover requires a 'title', this is missing it
    invalid_spec = {"cover": {"subhead": "Missing Title"}, "slides": []}

    result = await save_deck_spec(mock_context, invalid_spec)

    assert result.startswith("Error: Invalid deck_spec structure")
    # Verify nothing was saved to state or store
    assert "current_deck_spec" not in mock_context.state
    assert len(mock_context.store) == 0


# ==============================================================================
# Tests for update_slide_in_spec
# ==============================================================================


async def test_update_slide_in_spec_success(mock_context):
    """Test successfully updating an existing slide."""

    # 1. Setup an initial spec in state
    initial_spec = {
        "cover": {"title": "Test Cover"},
        "slides": [
            {
                "title": "Slide 1",
                "layout_name": "Title and Content",
                "bullets": [],
            },
            {
                "title": "Slide 2",
                "layout_name": "Title and Content",
                "bullets": [],
            },
        ],
        "closing_title": "End",
    }
    mock_context.state["current_deck_spec"] = initial_spec

    # 2. Perform the update
    update_data = {"title": "UPDATED Slide 2", "layout_name": "Title and Image"}

    result = await update_slide_in_spec(
        mock_context,
        1,  # Index 1 = Slide 2
        update_data,
    )

    assert "Success:" in result

    # 3. Verify the changes in state
    updated_spec = mock_context.state["current_deck_spec"]

    assert updated_spec["slides"][0]["title"] == "Slide 1"  # Unchanged
    assert updated_spec["slides"][1]["title"] == "UPDATED Slide 2"  # Changed
    assert (
        updated_spec["slides"][1]["layout_name"] == "Title and Image"
    )  # Changed


async def test_update_slide_in_spec_append_growth(mock_context):
    """Test successfully appending slides when index is beyond current length (Length Guardian)."""

    # 1. Setup initial spec (2 slides)
    initial_spec = {
        "cover": {"title": "Test Cover"},
        "slides": [
            {
                "title": "Slide 1",
                "layout_name": "Title and Content",
                "bullets": [],
            },
            {
                "title": "Slide 2",
                "layout_name": "Title and Content",
                "bullets": [],
            },
        ],
        "closing_title": "End",
    }
    mock_context.state["current_deck_spec"] = initial_spec

    # 2. Update index 3 (Slide 4). This should create Slide 3 (placeholder) and Slide 4 (actual).
    update_data = {"title": "Slide 4"}

    result = await update_slide_in_spec(mock_context, 3, update_data)

    assert "Success:" in result

    # 3. Verify growth
    updated_spec = mock_context.state["current_deck_spec"]
    assert len(updated_spec["slides"]) == 4
    assert (
        updated_spec["slides"][2]["title"] == "New Slide 3"
    )  # Auto-filled gap
    assert updated_spec["slides"][3]["title"] == "Slide 4"  # Appended target


async def test_update_slide_in_spec_not_found(mock_context):
    """Test updating a spec that doesn't exist."""

    result = await update_slide_in_spec(mock_context, 0, {"title": "Fail"})

    assert result.startswith("Error: No active presentation plan found")
