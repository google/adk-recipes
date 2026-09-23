# Copyright 2026 Google LLC
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presentation_agent.shared_libraries.config import (
    PRESENTATION_SPEC_ARTIFACT,
    RESEARCH_SUMMARY_ARTIFACT,
)
from presentation_agent.sub_agents.synthesizer.agent import (
    batch_generate_slides,
    generate_and_save_outline,
)
from presentation_agent.tools.presentation_orchestrator import (
    generate_and_render_deck,
)


class MockResponse:
    def __init__(self, text):
        self.text = text

    def __await__(self):
        async def _async_return():
            return self

        return _async_return().__await__()


@pytest.mark.asyncio
async def test_e2e_state_first_workflow():
    """
    Verifies the full Phase 2 -> Phase 4 flow using ONLY session state.
    Ensures that internal files (JSON/TXT) are NOT required as artifacts for the flow to succeed.
    """
    # Setup
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()  # We will track calls to this
    tool_context.load_artifact = AsyncMock(
        return_value=None
    )  # Ensure no artifact fallback is used

    mock_client = MagicMock()

    # --- PHASE 2: Generate Outline ---
    outline_json = {
        "outline": {
            "strategic_briefing": "Test Brief",
            "cover": {"title": "Test Presentation"},
            "slides": [
                {
                    "title": "Slide 1",
                    "layout_name": "Title and Content",
                    "bullets": ["Focus 1"],
                }
            ],
            "closing_title": "End",
        }
    }
    # generate_and_save_outline uses client.models.generate_content (SYNC)
    mock_client.models.generate_content.return_value = MockResponse(
        json.dumps(outline_json)
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        await generate_and_save_outline(
            tool_context=tool_context,
            topic="Test",
            slide_count=1,
            narrative_outline="Test Narrative",
            research_summary="Test Research [https://test.com]",
        )

    # Verify Phase 2 results in State
    assert "current_deck_spec" in tool_context.state
    assert "research_summary" in tool_context.state
    assert (
        tool_context.state["research_summary"]
        == "Test Research [https://test.com]"
    )
    print("✅ Phase 2: State Populated Successfully.")

    # --- PHASE 4 (Step 1): Batch Generate Slides ---
    # Mock the slide writer response
    slide_content_json = {
        "title": "Slide 1",
        "bullets": ["Detail 1", "Detail 2"],
        "layout_name": "Title and Content",
        "citations": ["https://test.com"],
    }
    mock_client.aio.models.generate_content.return_value = MockResponse(
        json.dumps(slide_content_json)
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        # NO arguments passed - must use state!
        result = await batch_generate_slides(tool_context=tool_context)

    assert result["status"] == "Success"
    assert tool_context.state["current_deck_spec"]["slides"][0]["bullets"] == [
        "Detail 1",
        "Detail 2",
    ]
    print("✅ Phase 4.1: Batch Content retrieved from State and updated State.")

    # --- PHASE 4 (Step 2): Render Deck ---
    # Mock template and visual generation
    with (
        patch(
            "presentation_agent.tools.presentation_orchestrator.get_gcs_file_as_local_path",
            AsyncMock(return_value="mock.pptx"),
        ),
        patch(
            "presentation_agent.tools.presentation_orchestrator.Presentation",
            MagicMock(),
        ),
        patch(
            "presentation_agent.tools.presentation_orchestrator.render_deck_from_spec",
            AsyncMock(return_value="final.pptx"),
        ),
        patch(
            "presentation_agent.tools.presentation_orchestrator.save_presentation",
            AsyncMock(return_value="Success"),
        ),
        patch(
            "presentation_agent.tools.presentation_orchestrator.os.remove",
            MagicMock(),
        ),
    ):
        # NO arguments passed - must use state!
        render_result = await generate_and_render_deck(
            tool_context=tool_context
        )

    assert render_result["status"] == "Success"
    print("✅ Phase 4.2: Renderer retrieved final spec from State.")

    # --- FINAL SIDEBAR CHECK ---
    # Verify that during the entire flow, no internal artifacts were saved
    # Note: save_presentation will still call save_artifact for the final PPTX, which is correct.
    artifact_names = [
        call.args[0] for call in tool_context.save_artifact.call_args_list
    ]

    # These should NOT be in the list anymore
    assert PRESENTATION_SPEC_ARTIFACT not in artifact_names
    assert RESEARCH_SUMMARY_ARTIFACT not in artifact_names
    print(
        "✅ UI Sidebar Cleanliness: Internal files were NOT registered as artifacts."
    )

    print("\n🚀 END-TO-END STATE-FIRST LOGIC VERIFIED.")
