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

"""Specialist agents for synthesizing research into a presentation deck spec."""

import asyncio
import json
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import AgentTool, FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ...shared_libraries.config import (
    PRESENTATION_SPEC_ARTIFACT,
    ROOT_MODEL,
    get_logger,
    initialize_genai_client,
)
from ...shared_libraries.models import (
    SlideSpec,
    SynthesizerResponse,
)
from .prompt import (
    SYNTHESIZER_OUTLINE_INSTRUCTION,
    SYNTHESIZER_SLIDE_INSTRUCTION,
)

# Agent 1: The Outliner (Creates the structural plan)
outline_specialist_agent = Agent(
    model=ROOT_MODEL,
    name="outline_specialist",
    mode="single_turn",
    description="A specialist agent that creates the high-level outline and strategic briefing for a presentation.",
    instruction=SYNTHESIZER_OUTLINE_INSTRUCTION,
    output_schema=SynthesizerResponse,
)

# Agent 2: The Slide Writer (Writes individual slides)
slide_writer_agent = Agent(
    model=ROOT_MODEL,
    name="slide_writer_specialist",
    mode="single_turn",
    description="A specialist agent that writes the detailed content (bullets, titles, visual prompts) for a single presentation slide.",
    instruction=SYNTHESIZER_SLIDE_INSTRUCTION,
    output_schema=SlideSpec,
)


async def generate_and_save_outline(
    tool_context: ToolContext,
    topic: str,
    slide_count: int,
    narrative_outline: str,
    research_summary: str,
) -> dict[str, Any]:
    """
    Consolidated tool that generates a presentation outline AND saves it to session state.
    Use this instead of calling outline_specialist and save_deck_spec separately.
    """
    log = get_logger("generate_and_save_outline")
    client = initialize_genai_client()

    # 1. Prepare the strategic prompt for the outliner
    prompt = (
        f"Topic: {topic}\n"
        f"Desired Slide Count: {slide_count}\n"
        f"Narrative Strategy: {narrative_outline}\n"
        f"Research Data: {research_summary}"
    )

    try:
        # 2. Call the Outliner. Using synchronous call to avoid event loop conflicts
        # when called from within another ADK Runner task.
        config = types.GenerateContentConfig(
            system_instruction=SYNTHESIZER_OUTLINE_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=SynthesizerResponse,
        )

        # models.generate_content is thread-safe and reliable for nested calls
        response = client.models.generate_content(
            model=ROOT_MODEL, contents=prompt, config=config
        )

        if not response or not response.text:
            raise RuntimeError("Outliner returned an empty response.")

        res_obj = SynthesizerResponse.model_validate_json(response.text)
        outline = res_obj.outline

        # 3. SAVE TO STATE (Invisible persistence)
        # This keeps the huge JSON out of the Orchestrator's immediate context
        tool_context.state["current_deck_spec"] = outline.model_dump()
        tool_context.state["research_summary"] = research_summary
        log.info(
            "Outline and research summary saved to session state successfully (invisible to UI)."
        )

        # 4. Return ONLY the data needed for the Markdown summary
        return {
            "status": "Success",
            "message": "Outline generated and securely saved to session state.",
            "strategic_briefing": outline.strategic_briefing,
            "cover": outline.cover.model_dump(),
            "slides": [s.model_dump() for s in outline.slides],
            "closing_title": outline.closing_title,
        }

    except Exception as e:
        log.error(f"Consolidated outline generation FAILED: {e}")
        return {"status": "Error", "message": str(e)}


async def batch_generate_slides(
    tool_context: ToolContext,
    *,
    research_summary: str | None = None,
    slides: list[dict[str, Any]] | None = None,
    spec_artifact_name: str | None = None,
) -> dict[str, Any]:
    """
    Generates detailed content for MULTIPLE slides in parallel with performance tuning.
    """
    log = get_logger("batch_generate_slides")
    client = initialize_genai_client()

    # 1. Resolve Research Summary (Priority: Session State > Direct Input)
    # We prioritize State to ensure the rich Phase 1 research (with URLs) isn't
    # overwritten by a generic summary provided during a revision turn.
    active_summary = tool_context.state.get("research_summary")

    if active_summary:
        log.info("Loaded rich research summary from session state.")
        # If a new summary was provided, we only update if it's significantly different/newer
        if (
            research_summary
            and len(research_summary) > len(active_summary) * 1.2
        ):
            active_summary = research_summary
            tool_context.state["research_summary"] = active_summary
            log.info(
                "Updated session state with a more substantial research summary."
            )
    elif research_summary:
        active_summary = research_summary
        tool_context.state["research_summary"] = active_summary
        log.info("Populated session state with provided research summary.")

    if not active_summary:
        return {
            "status": "Error",
            "message": "Missing research data. Please provide research_summary or ensure research was conducted.",
        }

    # 2. Resolve Slides (Priority: Direct List > Session State)
    active_slides = slides or []
    state_spec = None

    # Check Session State (Invisible persistence)
    if not active_slides:
        state_spec = tool_context.state.get("current_deck_spec")
        if state_spec:
            active_slides = state_spec.get("slides", [])
            log.info(f"Loaded {len(active_slides)} slides from session state.")

    if not active_slides:
        return {
            "status": "Error",
            "message": "No slide plan found in session state. Please provide slides or ensure an outline was generated.",
        }

    # 3. Performance Tuning: Allow 5 concurrent generations
    semaphore = asyncio.Semaphore(5)

    config = types.GenerateContentConfig(
        system_instruction=SYNTHESIZER_SLIDE_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=SlideSpec,
    )

    async def _generate_single_slide(topic: dict) -> dict:
        async with semaphore:
            t_title = topic.get("title", "Slide Content")
            t_layout = topic.get("layout_name", "Title and Content")
            bullets_field = topic.get("bullets", [])
            t_focus = (
                bullets_field[0]
                if bullets_field
                else "Focus on the topic provided in the title."
            )

            # RELIABILITY SANITIZATION
            visual_prompt = topic.get("visual_prompt")
            if isinstance(visual_prompt, str) and visual_prompt.lower() in [
                "none",
                "null",
                "n/a",
                "",
            ]:
                visual_prompt = None

            prompt = (
                f"Topic Focus: {t_focus}\n"
                f"Title: {t_title}\n"
                f"Planned Layout: {t_layout}\n"
                f"Planned Visual: {visual_prompt}\n"
                f"Research Summary: {active_summary}"
            )

            try:
                response = await client.aio.models.generate_content(
                    model=ROOT_MODEL, contents=prompt, config=config
                )

                if response and response.text:
                    res_dict = SlideSpec.model_validate_json(
                        response.text
                    ).model_dump(exclude_none=True)

                    # 1. PRESERVE CRITICAL FIELDS
                    res_dict["title"] = t_title

                    # 2. CITATION: If the outline already had citations, ensure they are kept
                    # unless the writer provided a more specific/updated list.
                    existing_citations = topic.get("citations")
                    if existing_citations and not res_dict.get("citations"):
                        res_dict["citations"] = existing_citations
                        log.info(
                            f"Recovered {len(existing_citations)} citations from outline for slide '{t_title}'."
                        )

                    # 3. SPEAKER NOTES: If the user provided manual notes (e.g. with citations),
                    # we want to preserve them. We prioritize model notes for the narrative,
                    # but append existing notes if they contain unique information.
                    existing_notes = topic.get("speaker_notes")
                    generated_notes = res_dict.get("speaker_notes", "")

                    if existing_notes:
                        if not generated_notes:
                            res_dict["speaker_notes"] = existing_notes
                            log.info(
                                f"Recovered existing speaker notes for slide '{t_title}'."
                            )
                        elif (
                            existing_notes.strip()
                            not in generated_notes.strip()
                        ):
                            res_dict["speaker_notes"] = (
                                generated_notes + "\n\n" + existing_notes
                            )
                            log.info(
                                f"Appended existing speaker notes to generated notes for slide '{t_title}'."
                            )

                    # Determine if we should keep the planned layout or override it
                    planned_layout = t_layout

                    # 1. Sanitize the generated visual prompt
                    # We prioritize the writer's decision. If it says null, there is NO visual.
                    generated_v = res_dict.get("visual_prompt")
                    if isinstance(generated_v, str) and generated_v.lower() in [
                        "none",
                        "null",
                        "n/a",
                        "",
                    ]:
                        generated_v = None
                        res_dict["visual_prompt"] = None

                    # 2. Decide if we have an effective visual to account for
                    # If generated_v is None, it means the writer explicitly chose NOT to have a visual.
                    has_visual = bool(generated_v)

                    if has_visual:
                        # If we have a visual, ensure the layout supports it.
                        if planned_layout not in [
                            "Title and Image",
                            "Two Content",
                            "Comparison",
                            "Content with Caption",
                            "Picture with Caption",
                        ]:
                            res_dict["layout_name"] = "Title and Image"
                        else:
                            res_dict["layout_name"] = planned_layout
                    # No visual, strictly follow planned layout unless it's an image-only layout
                    elif planned_layout in [
                        "Title and Image",
                        "Picture with Caption",
                    ]:
                        res_dict["layout_name"] = "Title and Content"
                    else:
                        res_dict["layout_name"] = planned_layout

                    return res_dict
            except Exception as e:
                return {
                    "title": t_title,
                    "layout_name": t_layout,
                    "visual_prompt": visual_prompt,
                    "bullets": [f"Error generating content: {e}"],
                }

        return {
            "title": t_title,
            "layout_name": t_layout,
            "visual_prompt": visual_prompt,
            "bullets": ["Error: Received empty response from model."],
        }

    # Execute all slide generations in parallel
    tasks = [_generate_single_slide(s) for s in active_slides]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    final_slides = []
    for r in results:
        if isinstance(r, Exception):
            final_slides.append(
                {
                    "title": "Error",
                    "layout_name": "Title and Content",
                    "bullets": [f"Error: {r!s}"],
                }
            )
        else:
            final_slides.append(r)

    # 4. SAVE TO STATE (Invisible persistence)
    # This replaces the placeholder slides from the outline with fully written content.
    state_spec = None
    if tool_context:
        # Load full spec to ensure we don't overwrite other fields (cover, briefing)
        state_spec = tool_context.state.get("current_deck_spec")
        if not state_spec:
            # Fallback check artifact if state is empty (Distributed Enterprise context)
            try:
                artifact = await tool_context.load_artifact(
                    PRESENTATION_SPEC_ARTIFACT
                )
                if artifact:
                    spec_bytes = (
                        artifact.inline_data.data
                        if isinstance(artifact, types.Part)
                        else artifact
                    )
                    state_spec = json.loads(spec_bytes.decode("utf-8"))
            except Exception:
                pass

        # FINAL FALLBACK: Ensure state_spec is never None before assignment
        if not state_spec:
            default_title = "Strategic Research Presentation"
            if final_slides:
                default_title = final_slides[0].get("title", default_title)
            state_spec = {
                "cover": {"title": default_title},
                "closing_title": "Thank You",
            }

        state_spec["slides"] = final_slides
        tool_context.state["current_deck_spec"] = state_spec
        log.info(
            f"Successfully saved {len(final_slides)} slides to session state (invisible to UI)."
        )

    return {
        "status": "Success",
        "message": f"Successfully generated content for {len(final_slides)} slides and saved to session state.",
        "slides_count": len(final_slides),
        "deck_spec": state_spec,
    }


# Create the tools to expose to the Orchestrator
outline_specialist_tool = AgentTool(agent=outline_specialist_agent)
generate_outline_and_save_tool = FunctionTool(func=generate_and_save_outline)
slide_writer_specialist_tool = AgentTool(agent=slide_writer_agent)
batch_slide_writer_tool = FunctionTool(func=batch_generate_slides)
