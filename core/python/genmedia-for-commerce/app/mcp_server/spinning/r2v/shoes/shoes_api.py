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

"""REST API endpoints for the shoes spinning R2V workflow."""

import base64
import io
import json
import logging
import multiprocessing
import os

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from google import genai
from workflows.shared.image_utils import stack_and_canvas_images
from workflows.shared.video_utils import merge_videos_from_bytes
from workflows.spinning.r2v.shoes.pipeline import (
    generate_single_clip_r2v,
    preprocess_classify_images,
    run_video_gen_pipeline_r2v,
)
from workflows.spinning.r2v.shoes.prompt_generation_r2v import (
    generate_veo_prompt_r2v,
)
from workflows.spinning.r2v.shoes.shoe_images_selection import (
    pick_images_by_ordered_best_side,
)

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

client = genai.Client(vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION)
veo_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)
shoe_classifier_model = os.getenv("SHOE_CLASSIFICATION_ENDPOINT")
gemini_model = os.getenv("MODEL_NAME_GENERATED_1")

router = APIRouter(
    prefix="/api/shoes/spinning",
    tags=["Shoes Spinning"],
)

# Gallery images directory
_script_dir = os.path.dirname(os.path.abspath(__file__))
_shoes_workflow_dir = os.path.normpath(
    os.path.join(
        _script_dir,
        os.pardir,
        os.pardir,
        os.pardir,
        os.pardir,
        "workflows",
        "spinning",
        "r2v",
        "shoes",
    )
)
_products_dir = os.path.join(_shoes_workflow_dir, "images", "products")


@router.get("/config")
async def get_shoes_config():
    """Return shoe spinning configuration status."""
    endpoint = shoe_classifier_model
    using_local = endpoint is None or endpoint in {"None", ""}
    return JSONResponse(content={"using_local_classifier": using_local})


@router.get("/get_gallery_images")
async def get_gallery_images():
    """Return a list of available gallery images grouped by product folder."""
    products = []

    if not os.path.exists(_products_dir):
        return JSONResponse(content={"products": []})

    try:
        for folder_name in sorted(os.listdir(_products_dir)):
            folder_path = os.path.join(_products_dir, folder_name)
            if os.path.isdir(folder_path) and folder_name.startswith(
                "product_"
            ):
                images = []
                for filename in sorted(os.listdir(folder_path)):
                    if filename.lower().endswith((".png", ".jpg", ".jpeg")):
                        image_url = f"/shoes/spinning/images/products/{folder_name}/{filename}"
                        images.append({"url": image_url, "name": filename})
                if images:
                    products.append(
                        {"folder_name": folder_name, "images": images}
                    )

        return JSONResponse(content={"products": products})
    except Exception as e:
        logger.error(f"Error scanning gallery images: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/preprocess-images-r2v")
async def preprocess_images_r2v(images: list[UploadFile] = File(...)):  # noqa: B008
    """Preprocesses shoe images for R2V pipeline and returns ordered images."""
    if not images:
        raise HTTPException(status_code=400, detail="No images provided.")

    try:
        images_bytes_list = [await image.read() for image in images]

        (
            images_classified,
            video_gen_status,
            has_velcro,
        ) = await run_in_threadpool(
            preprocess_classify_images,
            images_bytes_list=images_bytes_list,
            client=client,
            upscale_client=veo_client,
            shoe_classifier_model=shoe_classifier_model,
            num_workers=max(1, multiprocessing.cpu_count() - 1),
            upscale_images=True,
            create_canva=False,
        )

        if video_gen_status == "exclude":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Cannot generate video with these images",
                    "video_gen_status": video_gen_status,
                },
            )

        if has_velcro:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Cannot generate video for products with velcro closures",
                    "video_gen_status": "exclude",
                    "has_velcro": True,
                },
            )

        selected_ordered_images = pick_images_by_ordered_best_side(
            images_classified
        )
        ordered_classes = [x[1] for x in selected_ordered_images]
        images_picked = [x[0] for x in selected_ordered_images]

        reference_images, stacked_classes = stack_and_canvas_images(
            images_picked, ordered_classes
        )

        results = []
        for idx, (img_bytes, classification) in enumerate(
            zip(reference_images, stacked_classes, strict=False)
        ):
            results.append(
                {
                    "index": idx,
                    "prediction": str(classification),
                    "image_size": len(img_bytes),
                    "image_base64": base64.b64encode(img_bytes).decode("utf-8"),
                }
            )

        return JSONResponse(
            content={
                "results": results,
                "video_gen_status": video_gen_status,
                "num_images": len(results),
            }
        )
    except Exception as e:
        logger.error(f"Error in R2V preprocessing: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        for image in images:
            image.file.close()


@router.post("/generate-prompt-r2v")
async def generate_prompt_with_position_r2v(
    all_images: list[UploadFile] = File(...),  # noqa: B008
):
    """Generate R2V prompt and stacked reference images for video generation."""
    all_images_bytes = [await img.read() for img in all_images]

    try:
        prompt_text = await run_in_threadpool(
            generate_veo_prompt_r2v,
            client=client,
            gemini_model=gemini_model,
            all_images_bytes=all_images_bytes,
        )

        reference_images_data = []
        for idx, img_bytes in enumerate(all_images_bytes):
            reference_images_data.append(
                {
                    "index": idx,
                    "image_size": len(img_bytes),
                    "image_base64": base64.b64encode(img_bytes).decode("utf-8"),
                }
            )

        return JSONResponse(
            content={
                "prompt": prompt_text,
                "reference_images": reference_images_data,
                "num_reference_images": len(all_images_bytes),
                "original_num_images": len(all_images_bytes),
            }
        )
    except Exception as e:
        logger.error(f"Error during R2V prompt generation: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to generate R2V prompt: {e}"
        ) from e


@router.post("/generate-video-r2v")
async def generate_video_r2v(
    reference_images: list[UploadFile] = File(...),  # noqa: B008
    index: int = Form(0),
    prompt: str = Form(None),
    reference_type: str = Form("asset"),
    max_retries: int = Form(5),
):
    """Generate a single video using reference-to-video (R2V) modality."""
    try:
        reference_images_bytes = [await img.read() for img in reference_images]

        result = await run_in_threadpool(
            generate_single_clip_r2v,
            clip_idx=index,
            reference_images=reference_images_bytes,
            veo_client=veo_client,
            max_retries=max_retries,
            veo_model="veo-3.1-fast-generate-001",
            client=client,
            gemini_model=gemini_model,
            veo_prompt=prompt if prompt else None,
            reference_type=reference_type,
            shoe_classifier_model=shoe_classifier_model,
            validate_product_consistency_flag=False,
        )

        video_bytes = result["video_bytes"]
        if not video_bytes:
            raise Exception("R2V video generation returned empty bytes.")

        video_filename = f"vid_r2v_{index:04d}.mp4"
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={"X-Video-Filename": video_filename},
        )
    except Exception as e:
        logger.error(
            f"Error generating R2V video for index {index}: {e}", exc_info=True
        )
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
        logger.error(f"Error during merge: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        for video in videos:
            video.file.close()


@router.post("/run-pipeline-r2v")
async def run_pipeline_endpoint_r2v(payload: dict = Body(...)):  # noqa: B008
    """End-to-end R2V pipeline: classify, preprocess, and generate spinning video."""
    if "images_base64" not in payload:
        raise HTTPException(
            status_code=400, detail="Missing 'images_base64' in request body."
        )

    images_base64 = payload.get("images_base64", [])
    if not images_base64:
        raise HTTPException(status_code=400, detail="No images provided.")

    max_retries = payload.get("max_retries", 5)
    veo_model = payload.get("veo_model", "veo-3.1-generate-001")
    reference_type = payload.get("reference_type", "asset")
    upscale_images = payload.get("upscale_images", True)
    product_consistency_model = payload.get(
        "product_consistency_model", os.getenv("MODEL_NAME_GENERATED_4")
    )
    product_id = payload.get("product_id")
    gcs_bucket = payload.get("gcs_bucket")
    gcs_destination_prefix = payload.get(
        "gcs_destination_prefix", "shoe_spinning_outputs"
    )
    gcs_project_id = payload.get("gcs_project_id")
    disable_logging = payload.get("disable_logging", False)

    try:
        images_bytes_list = []
        for idx, img_b64 in enumerate(images_base64):
            try:
                images_bytes_list.append(base64.b64decode(img_b64))
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid base64 encoding for image {idx}: {e!s}",
                ) from e

        result = await run_in_threadpool(
            run_video_gen_pipeline_r2v,
            images_bytes_list=images_bytes_list,
            client=client,
            veo_client=veo_client,
            shoe_classifier_model=shoe_classifier_model,
            gemini_model=gemini_model,
            max_retries=max_retries,
            veo_model=veo_model,
            reference_type=reference_type,
            product_consistency_model=product_consistency_model,
            product_id=product_id,
            gcs_bucket=gcs_bucket,
            gcs_destination_prefix=gcs_destination_prefix,
            gcs_project_id=gcs_project_id,
            upscale_images=upscale_images,
            disable_logging=disable_logging,
        )

        if result is None:
            raise HTTPException(
                status_code=500, detail="Pipeline returned no result."
            )

        if isinstance(result, dict) and "video_bytes" in result:
            video_base64 = base64.b64encode(result["video_bytes"]).decode(
                "utf-8"
            )
            frames_base64 = [
                base64.b64encode(frame).decode("utf-8")
                for frame in result["frames"]
            ]
            return JSONResponse(
                content={
                    "video_base64": video_base64,
                    "frames_base64": frames_base64,
                    "num_frames": len(frames_base64),
                    "retry_count": result["retry_count"],
                }
            )
        else:
            return JSONResponse(
                content={
                    "video_gen_status": result["video_gen_status"],
                    "num_clips": len(result["clips"]),
                    "clips": result["clips"],
                    "gcs_uris": result["gcs_uris"],
                    "gcs_base_uri": f"gs://{gcs_bucket}/{gcs_destination_prefix}/{product_id}/",
                }
            )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error in R2V pipeline: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
