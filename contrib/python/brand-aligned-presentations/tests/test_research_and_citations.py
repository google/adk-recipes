# Copyright 2026 Google LLC
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presentation_agent.shared_libraries.models import (
    CoverSpec,
    PresentationOutline,
    SlideSpec,
)
from presentation_agent.sub_agents.synthesizer.agent import (
    batch_generate_slides,
    generate_and_save_outline,
)
from presentation_agent.tools.presentation_orchestrator import (
    render_deck_from_spec,
)


class MockResponse:
    def __init__(self, text):
        self.text = text


@pytest.mark.asyncio
async def test_full_citation_workflow():
    # --- STEP 1: Research Summary Mock ---
    research_summary = (
        "Finding 1: Global renewable energy market reached $2.15 trillion in 2023 "
        "[https://www.grandviewresearch.com/industry-analysis/renewable-energy-market].\n"
        "Finding 2: Market growth is projected at 12% CAGR [https://example-source.com/report]."
    )
    print("\n[Step 1] Research Summary simulated with inline citations.")

    # --- STEP 2: Outline Generation Verification ---
    tool_context = MagicMock()
    tool_context.state = {}

    mock_outline = PresentationOutline(
        strategic_briefing="Strategic Briefing text",
        cover=CoverSpec(title="Future of Energy", subhead="Analysis 2026"),
        slides=[
            SlideSpec(
                title="Market Overview",
                bullets=["Summary of market size"],
                layout_name="Title and Content",
                citations=[
                    "https://www.grandviewresearch.com/industry-analysis/renewable-energy-market"
                ],
            )
        ],
        closing_title="Thank You",
    )

    mock_outliner_response = MockResponse(
        json.dumps({"outline": mock_outline.model_dump()})
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client"
    ) as mock_init:
        mock_client = mock_init.return_value
        mock_client.models.generate_content.return_value = (
            mock_outliner_response
        )

        result = await generate_and_save_outline(
            tool_context=tool_context,
            topic="Renewable Energy",
            slide_count=1,
            narrative_outline="Strategic Arc",
            research_summary=research_summary,
        )

    assert result["status"] == "Success"
    assert result["slides"][0]["citations"] == [
        "https://www.grandviewresearch.com/industry-analysis/renewable-energy-market"
    ]
    print(
        "[Step 2] Outline generation verified: Citations extracted from summary into SlideSpec."
    )

    # --- STEP 3: Slide Generation Verification ---
    mock_written_slide = SlideSpec(
        title="Market Overview",
        bullets=[
            "The global renewable energy market hit $2.15 trillion in 2023."
        ],
        layout_name="Title and Content",
        citations=[
            "https://www.grandviewresearch.com/industry-analysis/renewable-energy-market"
        ],
        speaker_notes="Market growth is substantial.",
    )

    mock_writer_response = MockResponse(
        json.dumps(mock_written_slide.model_dump())
    )

    with patch(
        "presentation_agent.sub_agents.synthesizer.agent.initialize_genai_client"
    ) as mock_init:
        mock_client = mock_init.return_value
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=mock_writer_response
        )

        result = await batch_generate_slides(
            tool_context=tool_context,
            research_summary=research_summary,
            slides=result["slides"],
        )

    assert result["status"] == "Success"
    final_slide = result["deck_spec"]["slides"][0]
    assert final_slide["citations"] == [
        "https://www.grandviewresearch.com/industry-analysis/renewable-energy-market"
    ]
    print(
        "[Step 3] Slide generation verified: Citations preserved in final DeckSpec."
    )

    # --- STEP 4: Render Verification ---
    deck_spec_dict = result["deck_spec"]

    with patch(
        "presentation_agent.tools.presentation_orchestrator.Presentation"
    ) as mock_prs_class:
        mock_prs = mock_prs_class.return_value

        # Mock slides collection
        mock_cover_slide = MagicMock()
        mock_body_slide = MagicMock()
        mock_closing_slide = MagicMock()

        # Simulate prs.slides as a list-like object with add_slide
        mock_slides_collection = [mock_cover_slide]
        mock_prs.slides = mock_slides_collection
        # Add attributes to the list object (Python allows this for MagicMock but not standard list)
        mock_prs.slides = MagicMock(spec=list)
        mock_prs.slides.__len__.return_value = 1
        mock_prs.slides.__getitem__.side_effect = lambda i: [mock_cover_slide][
            i
        ]
        mock_prs.slides.__iter__.side_effect = lambda: iter([mock_cover_slide])
        mock_prs.slides.add_slide = MagicMock(
            side_effect=[mock_body_slide, mock_closing_slide]
        )
        mock_prs.slides._sldIdLst = [MagicMock()]

        # Mock notes_slide structure
        mock_notes_slide = MagicMock()
        mock_body_slide.notes_slide = mock_notes_slide

        # Mock other required structures
        mock_prs.part = MagicMock()

        with patch("os.path.exists", return_value=True):
            with patch(
                "presentation_agent.tools.presentation_orchestrator.get_smart_layout"
            ):
                with patch(
                    "presentation_agent.tools.presentation_orchestrator._insert_image"
                ):
                    with patch("tempfile.NamedTemporaryFile") as mock_temp:
                        mock_temp.return_value.__enter__.return_value.name = (
                            "dummy.pptx"
                        )

                        await render_deck_from_spec(
                            spec_dict=deck_spec_dict,
                            out_pptx="output.pptx",
                            tool_context=tool_context,
                            template_pptx="template.pptx",
                        )

        # Verify citations were appended to notes
        assigned_notes = mock_notes_slide.notes_text_frame.text
        assert "Citations:" in assigned_notes
        assert (
            "https://www.grandviewresearch.com/industry-analysis/renewable-energy-market"
            in assigned_notes
        )
        print(
            "[Step 4] Render verified: Citations appended to speaker notes in PPTX."
        )

    print(
        "\n✅ ALL STEPS VERIFIED: Citations correctly flow from Research -> Outline -> Slide -> PPTX."
    )


if __name__ == "__main__":
    asyncio.run(test_full_citation_workflow())
