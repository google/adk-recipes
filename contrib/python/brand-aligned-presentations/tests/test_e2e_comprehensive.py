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
async def test_comprehensive_e2e_flow():
    """
    Rigorously verifies:
    1. Mandatory Citation Extraction from State
    2. State-First Data Persistence (current_deck_spec, research_summary)
    3. UI Cleanliness (No internal JSON/TXT artifacts)
    4. Final Deliverable Visibility (.pptx MUST be an artifact)
    """
    # Setup
    tool_context = MagicMock()
    tool_context.state = {}
    tool_context.save_artifact = AsyncMock()
    tool_context.load_artifact = AsyncMock(return_value=None)

    mock_client = MagicMock()

    # --- PHASE 2: Generate Outline (Memory Setup) ---
    outline_json = {
        "outline": {
            "strategic_briefing": "Growth strategy",
            "cover": {"title": "Biotech 2026"},
            "slides": [
                {
                    "title": "Market Trends",
                    "layout_name": "Title and Content",
                    "bullets": ["Focus on growth"],
                }
            ],
            "closing_title": "End",
        }
    }
    # generate_and_save_outline uses client.models.generate_content (SYNC)
    mock_client.models.generate_content.return_value = MockResponse(
        json.dumps(outline_json)
    )

    research_text = "Market growth is 15% [https://market-data.com/2026]. AI is accelerating drug discovery (Source: https://science-hub.org/ai)."

    print("\n--- Phase 2: Synthesis ---")
    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        await generate_and_save_outline(
            tool_context=tool_context,
            topic="Biotech",
            slide_count=1,
            narrative_outline="Arc",
            research_summary=research_text,
        )

    assert "current_deck_spec" in tool_context.state
    assert "research_summary" in tool_context.state
    print("✅ State Populated (DeckSpec & Summary)")

    # --- PHASE 4.1: Batch Content Generation (Citation Verification) ---
    slide_content_json = {
        "title": "Market Trends",
        "bullets": ["Growth is 15% in 2026.", "AI speeds up R&D."],
        "layout_name": "Title and Content",
        "citations": [
            "https://market-data.com/2026",
            "https://science-hub.org/ai",
        ],
    }
    mock_client.aio.models.generate_content.return_value = MockResponse(
        json.dumps(slide_content_json)
    )

    print("--- Phase 4.1: Batch Generation ---")
    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client",
        return_value=mock_client,
    ):
        # Must retrieve research and plan from state!
        result = await batch_generate_slides(tool_context=tool_context)

    assert result["status"] == "Success"
    generated_slide = tool_context.state["current_deck_spec"]["slides"][0]
    assert len(generated_slide["citations"]) == 2
    assert "https://market-data.com/2026" in generated_slide["citations"]
    print("✅ Citations extracted correctly from State Summary")

    # --- PHASE 4.2: Rendering (Final Deliverable Verification) ---
    print("--- Phase 4.2: Rendering ---")

    # Mock the save_presentation function to simulate its core behavior
    async def mock_save_pres(ctx, name, path, bucket):
        await ctx.save_artifact(name, "mock_blob")
        return "Success"

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
            AsyncMock(return_value="final_output.pptx"),
        ),
        patch(
            "presentation_agent.tools.presentation_orchestrator.save_presentation",
            side_effect=mock_save_pres,
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
    print("✅ Final PPTX Rendered and Saved.")

    # --- ARTIFACT AUDIT (UI Verification) ---
    artifact_names = [
        call.args[0] for call in tool_context.save_artifact.call_args_list
    ]

    print("\n--- UI Sidebar Cleanliness Audit ---")
    # 1. Internal files MUST NOT be there
    assert PRESENTATION_SPEC_ARTIFACT not in artifact_names
    assert RESEARCH_SUMMARY_ARTIFACT not in artifact_names
    print("✅ PASS: Internal JSON/TXT files are HIDDEN (State-only).")

    # 2. Final PPTX MUST be there
    pptx_found = any(name.endswith(".pptx") for name in artifact_names)
    assert pptx_found
    print("✅ PASS: Final presentation VISIBLE in sidebar.")

    print("\n🚀 ALL COMPREHENSIVE E2E VERIFICATIONS PASSED.")
