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

"""FastAPI backend server for the VTO catalog sandbox dashboard.

Serves catalog data and runs virtual try-on image and video requests.
"""

import base64
import logging
import os
import shutil
import uuid
from pathlib import Path

import google.auth as google_auth
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import storage
from pydantic import BaseModel

from scripts.config import config
from scripts.scan_catalog import scan_directory
from scripts.tryon_processor import generate_tryon_image, generate_tryon_video

os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Virtual Try-On Sandbox API")

# Enable CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_project() -> str:
    """Resolve GCP project: env var, then ADC default. Raises if neither set."""
    if config.GOOGLE_CLOUD_PROJECT:
        return config.GOOGLE_CLOUD_PROJECT
    try:
        _, adc_proj = google_auth.default()
    except Exception:
        adc_proj = None
    if not adc_proj:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT not set and no ADC project available. "
            "Copy `.env.example` to `.env` and set GOOGLE_CLOUD_PROJECT, "
            "or run the skill's Q-MODE which writes it for you."
        )
    return adc_proj


PROJECT_ID = _resolve_project()
LOCATION = config.GCP_REGION
MODEL_LOCATION = config.GEMINI_MODEL_LOCATION
OUTPUT_BUCKET = config.TRYON_OUTPUT_BUCKET
MODEL_NAME = config.GEMINI_IMAGE_MODEL
DEFAULT_CATALOG_PATH = config.TRYON_CATALOG_PATH

TMP_UPLOAD_DIR = Path("tmp/vto-uploads")
TMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class VideoRequest(BaseModel):
    tryon_image_base64: str
    scene_description: str = "a minimal fashion studio catwalk walk"


@app.get("/api/catalog")
def get_catalog(path: str | None = None, force: bool = False):
    """Retrieve scanned product catalog."""
    catalog_path = path or DEFAULT_CATALOG_PATH
    is_gcs = catalog_path.startswith("gs://")

    if not is_gcs:
        catalog_dir = Path(catalog_path)
        if not catalog_dir.exists():
            # Fallback to create sample catalog if default is requested
            if not path:
                catalog_dir.mkdir(exist_ok=True)
                # Create a mock catalog inside it
                from scripts.setup_tryon import setup_sample_catalog

                setup_sample_catalog()
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Catalog path '{catalog_path}' does not exist.",
                )

    try:
        catalog = scan_directory(
            catalog_path, PROJECT_ID, LOCATION, force_reindex=force
        )
        path_display = (
            catalog_path if is_gcs else str(Path(catalog_path).resolve())
        )
        return {
            "status": "success",
            "catalog_path": path_display,
            "products": catalog,
        }
    except Exception as e:
        logger.error(f"Catalog scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/media")
def get_media(
    path: str = Query(..., description="GCS media URI: gs://bucket/name"),
):
    """Proxy stream GCS files on-demand without writing to local disk."""
    if not path.startswith("gs://"):
        raise HTTPException(
            status_code=400,
            detail="Only gs:// paths are supported for streaming.",
        )

    try:
        gcs_path = path[5:]
        bucket_name, blob_name = gcs_path.split("/", 1)
        gcs_client = storage.Client(project=PROJECT_ID)
        bucket = gcs_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        if not blob.exists():
            raise HTTPException(
                status_code=404, detail="Media file not found in GCS."
            )

        def stream_file():
            with blob.open("rb") as f:
                while chunk := f.read(1024 * 256):
                    yield chunk

        # Match content types
        content_type = "image/jpeg"
        if blob_name.endswith(".png"):
            content_type = "image/png"
        elif blob_name.endswith(".webp"):
            content_type = "image/webp"

        return StreamingResponse(stream_file(), media_type=content_type)
    except HTTPException:
        # Preserve deliberate HTTP responses (e.g. the 404 above) so they
        # reach the client with their original status; only unexpected
        # errors should fall through to the 500 handler.
        raise
    except Exception as e:
        logger.error(f"Media streaming failed for '{path}': {e}")
        raise HTTPException(
            status_code=500, detail=f"Streaming error: {e}"
        ) from e


@app.post("/api/tryon")
async def tryon_image(
    product_id: str = Form(...),
    product_category: str = Form("Clothing"),
    product_description: str = Form(""),
    product_image_path: str = Form(...),
    user_photo: UploadFile = File(...),  # noqa: B008 -- FastAPI DI relies on File() as a default sentinel
):
    """Process user photo + product to generate try-on composite."""
    # 1. Save uploaded user photo locally
    user_photo_filename = f"user_{uuid.uuid4().hex[:8]}_{user_photo.filename}"
    user_photo_path = TMP_UPLOAD_DIR / user_photo_filename

    try:
        with open(user_photo_path, "wb") as buffer:
            shutil.copyfileobj(user_photo.file, buffer)
    except Exception as e:
        logger.error(f"Failed to save uploaded user photo: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to upload user image: {e}"
        ) from e

    # 2. Resolve product image path
    temp_prod_path = None
    if "path=gs://" in product_image_path:
        import urllib.parse

        parsed_url = urllib.parse.urlparse(product_image_path)
        queries = urllib.parse.parse_qs(parsed_url.query)
        gs_uri = queries.get("path", [None])[0]

        if not gs_uri or not gs_uri.startswith("gs://"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid GCS media proxy path: {product_image_path}",
            )

        try:
            logger.info(
                f"Downloading product image from GCS proxy path: {gs_uri}"
            )
            gcs_path = gs_uri[5:]
            bucket_name, blob_name = gcs_path.split("/", 1)
            gcs_client = storage.Client(project=PROJECT_ID)
            bucket = gcs_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)

            temp_prod_filename = (
                f"prod_{uuid.uuid4().hex[:8]}_{Path(blob_name).name}"
            )
            temp_prod_path = TMP_UPLOAD_DIR / temp_prod_filename
            blob.download_to_filename(temp_prod_path)
            product_image_realpath = str(temp_prod_path.resolve())
        except Exception as e:
            logger.error(f"Failed to fetch product from GCS: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch product image from GCS: {e}",
            ) from e
    else:
        workspace_prod_path = Path(product_image_path)
        if not workspace_prod_path.exists():
            workspace_prod_path = (
                Path(DEFAULT_CATALOG_PATH).parent / product_image_path
            )
        if not workspace_prod_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Product image path '{product_image_path}' not found.",
            )
        product_image_realpath = str(workspace_prod_path.resolve())

    logger.info(
        f"Triggering VTO image generation for product: {product_id} ({product_category})"
    )

    try:
        res = generate_tryon_image(
            person_image_path=str(user_photo_path.resolve()),
            product_image_path=product_image_realpath,
            project_id=PROJECT_ID,
            output_bucket=OUTPUT_BUCKET,
            model_name=MODEL_NAME,
            product_category=product_category,
            product_description=product_description,
            location=MODEL_LOCATION,
        )

        # Convert output image to base64 for instant frontend rendering
        result_bytes = res["image_bytes"]
        result_base64 = base64.b64encode(result_bytes).decode("utf-8")

        return {
            "status": "success",
            "image_base64": f"data:image/jpeg;base64,{result_base64}",
            "output_uri": res.get("output_uri", ""),
            "model_used": res["model_used"],
        }
    except Exception as e:
        logger.error(f"VTO Image generation failed: {e}")
        err_msg = str(e).lower()
        if "timed out" in err_msg or "readtimeout" in err_msg:
            detail_msg = "Vertex AI Image generation endpoint is currently overloaded or experiencing high latency. Please wait a few moments and click Try-On again."
        elif "503" in err_msg or "service unavailable" in err_msg:
            detail_msg = "Vertex AI VTO service is temporarily unavailable. Please try again in a few moments."
        elif "429" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            detail_msg = "Vertex AI rate limits or quotas exceeded for VTO models. Please try again shortly."
        else:
            detail_msg = f"Generation failed: {e}"
        raise HTTPException(status_code=500, detail=detail_msg) from e
    finally:
        # Clean up temp upload file
        if user_photo_path.exists():
            user_photo_path.unlink()
        if temp_prod_path and temp_prod_path.exists():
            temp_prod_path.unlink()


@app.post("/api/video")
async def tryon_video(req: VideoRequest):
    """Animate a try-on composite image into a catwalk video."""
    logger.info("Triggering Veo video generation catwalk walk...")
    try:
        # Decode base64 image
        _header, base64_data = req.tryon_image_base64.split(",", 1)
        image_bytes = base64.b64decode(base64_data)

        res = generate_tryon_video(
            tryon_image_bytes=image_bytes,
            project_id=PROJECT_ID,
            output_bucket=OUTPUT_BUCKET,
            scene_description=req.scene_description,
            location=MODEL_LOCATION,
        )

        video_bytes = res["video_bytes"]
        video_base64 = base64.b64encode(video_bytes).decode("utf-8")

        return {
            "status": "success",
            "video_base64": f"data:video/mp4;base64,{video_base64}",
            "output_uri": res.get("output_uri", ""),
        }
    except Exception as e:
        logger.error(f"VTO Video generation failed: {e}")
        err_msg = str(e).lower()
        if "timed out" in err_msg or "readtimeout" in err_msg:
            detail_msg = "Veo video generation endpoint is currently overloaded or experiencing high latency. Please wait a few moments and click Generate Catwalk again."
        elif "503" in err_msg or "service unavailable" in err_msg:
            detail_msg = "Veo VTO service is temporarily unavailable. Please try again in a few moments."
        elif "429" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            detail_msg = "Vertex AI rate limits or quotas exceeded for Veo video models. Please try again shortly."
        else:
            detail_msg = f"Video generation failed: {e}"
        raise HTTPException(status_code=500, detail=detail_msg) from e


# Mount catalog_images and .catalog_cache directories to serve catalog images to browser
catalog_images_dir = Path("catalog_images")
catalog_images_dir.mkdir(exist_ok=True)
app.mount(
    "/catalog_images",
    StaticFiles(directory="catalog_images"),
    name="catalog_images",
)

catalog_cache_dir = Path(".catalog_cache")
catalog_cache_dir.mkdir(exist_ok=True)
app.mount(
    "/.catalog_cache",
    StaticFiles(directory=".catalog_cache"),
    name="catalog_cache",
)

# Mount static files UI at "/"
# Locate static folder dynamically relative to the server script
UI_DIR = Path(__file__).resolve().parent.parent / "assets" / "ui"
if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="static")
else:
    logger.warning(
        f"UI directory not found at: {UI_DIR}. API server will run headless."
    )
