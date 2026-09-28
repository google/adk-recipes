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

"""REST API endpoints for the glasses video VTO workflow."""

import io
import json
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from google import genai
from workflows.shared.video_utils import merge_videos_from_bytes
from workflows.video_vto.glasses.custom_template import (
    generate_animation_prompt,
    generate_custom_template,
)
from workflows.video_vto.glasses.pipeline import (
    RegenerationRequest,
    run_generation_pipeline,
    run_regeneration_pipeline,
)

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

genai_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)

router = APIRouter(
    prefix="/api/glasses",
    tags=["Glasses Video VTO"],
)

# Templates directory
_script_dir = os.path.dirname(os.path.abspath(__file__))
_glasses_video_dir = os.path.normpath(
    os.path.join(
        _script_dir,
        os.pardir,
        os.pardir,
        os.pardir,
        "workflows",
        "video_vto",
        "glasses",
    )
)


@router.get("/get_templates")
async def get_templates():
    """Return a list of available template videos with paths and empty prompts."""
    men_templates_path = os.path.join(_glasses_video_dir, "men_templates.jsonl")
    women_templates_path = os.path.join(
        _glasses_video_dir, "women_templates.jsonl"
    )

    men_videos = []
    if os.path.exists(men_templates_path):
        with open(men_templates_path) as f:
            for line in f:
                men_videos.append(json.loads(line))

    women_videos = []
    if os.path.exists(women_templates_path):
        with open(women_templates_path) as f:
            for line in f:
                women_videos.append(json.loads(line))

    return JSONResponse(content={"men": men_videos, "women": women_videos})


@router.post("/generate-prompt")
async def generate_prompt_from_json(request: Request):
    """Generate a single prompt string from a JSON object."""
    try:
        prompt_data = await request.json()

        has_model = prompt_data.get("has_model_image", False)
        default_transition = (
            "Instantly transition to:"
            if has_model
            else "Instantly turn off the scene."
        )

        transition_sentence = prompt_data.get(
            "transition_sentence", default_transition
        )
        prompt_text = f"{transition_sentence}\n"

        prompt_text += "\n".join(
            f"**{key.replace('_', ' ').capitalize()}:** {value}"
            for key, value in prompt_data.items()
            if key not in ["transition_sentence", "has_model_image"]
        )
        return JSONResponse(content={"prompt": prompt_text})
    except Exception as e:
        logger.error(f"Error in /generate-prompt: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e


@router.post("/generate-custom-prompt")
async def generate_custom_prompt(
    text: str = Form(...),
    model_image: UploadFile | None = File(None),  # noqa: B008
    product_image: UploadFile | None = File(None),  # noqa: B008
    custom_field_dict: str = Form(None),
):
    """Generate a structured prompt from user's natural language text and images."""
    try:
        model_image_bytes = await model_image.read() if model_image else None
        product_image_bytes = (
            await product_image.read() if product_image else None
        )

        parsed_custom_field_dict = None
        if custom_field_dict:
            try:
                parsed_custom_field_dict = json.loads(custom_field_dict)
            except json.JSONDecodeError:
                logger.warning(
                    f"Failed to parse custom_field_dict: {custom_field_dict}"
                )

        result = await run_in_threadpool(
            generate_custom_template,
            genai_client=genai_client,
            text_prompt=text,
            custom_field_dict=parsed_custom_field_dict,
            model_image_bytes=model_image_bytes,
            product_image_bytes=product_image_bytes,
        )

        json_response = json.loads(result)

        if "transition_sentence" not in json_response or not json_response.get(
            "transition_sentence"
        ):
            default_transition = (
                "Instantly transition to:"
                if model_image_bytes
                else "Instantly turn off the scene."
            )
            json_response["transition_sentence"] = default_transition

        json_response = {
            k: v
            for k, v in json_response.items()
            if v is not None
            and not (isinstance(v, str) and len(v.strip()) == 0)
        }
        return JSONResponse(content=json_response)
    except Exception as e:
        logger.error(f"Error in /generate-custom-prompt: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e


@router.post("/generate-animation-prompt")
async def generate_animation_prompt_endpoint(
    text: str = Form(...),
    model_image: UploadFile | None = File(None),  # noqa: B008
):
    """Generate an enhanced animation prompt from user's text and model image."""
    try:
        model_image_bytes = await model_image.read() if model_image else None

        enhanced_prompt = await run_in_threadpool(
            generate_animation_prompt,
            genai_client=genai_client,
            text_prompt=text,
            model_image_bytes=model_image_bytes,
        )

        return JSONResponse(content={"enhanced_prompt": enhanced_prompt})
    except Exception as e:
        logger.error(f"Error in /generate-animation-prompt: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e


@router.post("/generate-video")
async def generate_video_from_collage(
    prompt: str = Form(""),
    model_image: UploadFile | None = File(None),  # noqa: B008
    model_side_image: UploadFile | None = File(None),  # noqa: B008
    product_image: UploadFile | None = File(None),  # noqa: B008
    number_of_videos: int = Form(4),
    is_template_product_image: str = Form("false"),
    background_color: str = Form("0,215,6,255"),
    zoom_level: int = Form(0),
    is_animation_mode: str = Form("false"),
):
    """Generate videos from a collage of images or animate a single image."""
    try:
        model_image_bytes = await model_image.read() if model_image else None
        model_side_image_bytes = (
            await model_side_image.read() if model_side_image else None
        )
        product_image_bytes = (
            await product_image.read() if product_image else None
        )
        is_template_product_image_bool = (
            is_template_product_image.lower() == "true"
        )
        is_animation_mode_bool = is_animation_mode.lower() == "true"

        try:
            bg_color = tuple(map(int, background_color.split(",")))
        except ValueError:
            bg_color = (0, 215, 6, 255)

        zoom_level = max(0, min(6, zoom_level))

        result = await run_in_threadpool(
            run_generation_pipeline,
            prompt,
            number_of_videos,
            model_image_bytes,
            product_image_bytes,
            model_side_image_bytes=model_side_image_bytes,
            is_template_product_image=is_template_product_image_bool,
            bg_color=bg_color,
            zoom_level=zoom_level,
            is_animation_mode=is_animation_mode_bool,
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error in /generate-video: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e


@router.post("/regenerate-video")
async def regenerate_video(req: RegenerationRequest):
    """Regenerates a video from existing collage data."""
    try:
        result = await run_in_threadpool(run_regeneration_pipeline, req)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error in /regenerate-video: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e


@router.post("/merge-videos")
async def merge_videos(
    videos: list[UploadFile] = File(...),  # noqa: B008
    speeds: str = Form(...),
):
    """Merge multiple video files into one."""
    if not videos:
        raise HTTPException(status_code=400, detail="No video files provided.")

    try:
        video_bytes_list = [await video.read() for video in videos]
        speed_list = json.loads(speeds)

        merged_video_bytes = await run_in_threadpool(
            merge_videos_from_bytes,
            videos_bytes=video_bytes_list,
            speeds=speed_list,
        )

        return StreamingResponse(
            io.BytesIO(merged_video_bytes),
            media_type="video/mp4",
            headers={
                "Content-Disposition": "attachment; filename=merged_video.mp4"
            },
        )
    except Exception as e:
        logger.error(f"Error in /merge-videos: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        for video in videos:
            video.file.close()
