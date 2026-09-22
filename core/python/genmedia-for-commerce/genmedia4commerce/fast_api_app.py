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

"""Combined FastAPI application.

Serves:
1. ADK agent API (chat, sessions, etc.) via adk's get_fast_api_app
2. REST API endpoints (for frontend compatibility) via mcp_server routers
3. Frontend static files
"""

import os
import sys
from pathlib import Path

import google.auth
from app_utils.telemetry import setup_telemetry
from app_utils.typing import Feedback
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging

# Ensure genmedia4commerce/ is on the path for imports
GENMEDIA_DIR = Path(__file__).parent
PROJECT_ROOT = GENMEDIA_DIR.parent
if str(GENMEDIA_DIR) not in sys.path:
    sys.path.insert(0, str(GENMEDIA_DIR))

# Load config.env
from dotenv import load_dotenv  # noqa: E402

config_path = GENMEDIA_DIR / "config.env"
if config_path.exists():
    load_dotenv(config_path)

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO)

setup_telemetry()
try:
    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
except google.auth.exceptions.DefaultCredentialsError:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    import logging as _std_logging

    logger = _std_logging.getLogger(__name__)  # type: ignore[assignment]

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",")
    if os.getenv("ALLOW_ORIGINS")
    else None
)

logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

# ADK agents directory — contains agents/ subdirectory with agent.py
AGENT_DIR = str(GENMEDIA_DIR)
session_service_uri = None
artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

# Create ADK FastAPI app (provides /run, /apps/*, sessions)
# web=True enables the ADK dev UI — only for local development
app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=os.getenv("ADK_WEB_UI", "false").lower() == "true",
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins or ["*"],
    session_service_uri=session_service_uri,
    otel_to_cloud=not os.getenv("INTEGRATION_TEST")
    and os.getenv("USE_IN_MEMORY_SESSION") not in ("true", "1", "True", "TRUE"),
)
app.title = "GenMedia for Commerce"
app.description = "ADK Agent + REST API for GenMedia workflows"

# --- REST API routers (replace legacy mounts) ---
from chat_api import router as chat_router  # noqa: E402
from mcp_server.image_vto.clothes.clothes_api import (  # noqa: E402
    router as clothes_image_router,
)
from mcp_server.image_vto.glasses.glasses_api import (  # noqa: E402
    router as glasses_image_router,
)
from mcp_server.other.background_changer.background_changer_api import (  # noqa: E402
    router as background_changer_router,
)
from mcp_server.product_enrichment.product_fitting.product_fitting_api import (  # noqa: E402
    router as product_fitting_router,
)
from mcp_server.shared.catalog.catalog_api import (  # noqa: E402
    router as catalog_router,
)
from mcp_server.spinning.interpolation.other.other_api import (  # noqa: E402
    router as interpolation_other_router,
)
from mcp_server.spinning.r2v.other.other_api import (  # noqa: E402
    router as r2v_other_router,
)
from mcp_server.spinning.r2v.shoes.shoes_api import (  # noqa: E402
    router as shoes_spinning_router,
)
from mcp_server.video_vto.clothes.clothes_api import (  # noqa: E402
    router as clothes_video_router,
)
from mcp_server.video_vto.glasses.glasses_api import (  # noqa: E402
    router as glasses_video_router,
)

app.include_router(product_fitting_router)
app.include_router(clothes_image_router)
app.include_router(glasses_image_router)
app.include_router(clothes_video_router)
app.include_router(glasses_video_router)
app.include_router(background_changer_router)
app.include_router(shoes_spinning_router)
app.include_router(r2v_other_router)
app.include_router(interpolation_other_router)
app.include_router(catalog_router)
app.include_router(chat_router)


# --- Feedback endpoint (ASP standard) ---
@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback."""
    try:
        logger.log_struct(feedback.model_dump(), severity="INFO")
    except Exception as exc:
        # Cloud Logging may be unavailable (no credentials, wrong project,
        # insufficient IAM permissions, etc.).  Fall back silently so the
        # endpoint always returns 200 — feedback is never worth a 500.
        import logging as _std_logging

        _std_logging.getLogger(__name__).warning(
            "Cloud Logging write failed (%s); feedback dropped.", exc
        )
    return {"status": "success"}


# --- Startup: start MCP server ---
# Note: catalogue is pre-loaded at import time via vector_search module
import threading  # noqa: E402

from mcp_server.server import server as mcp_server  # noqa: E402

try:
    _mcp_port = int(os.getenv("MCP_SERVER_PORT", "8081"))
except (ValueError, TypeError):
    _mcp_port = 8081


def _run_mcp():
    mcp_server.settings.port = _mcp_port
    mcp_server.run(transport="sse")


threading.Thread(target=_run_mcp, daemon=True).start()


# --- Frontend static files (must be at module level, not in startup event) ---
def _mount_frontend():
    """Mount frontend static files if they exist."""
    from fastapi.staticfiles import StaticFiles

    frontend_dir = PROJECT_ROOT / "frontend" / "dist"
    if not frontend_dir.exists():
        return

    for subdir in ["assets", "products", "templates"]:
        dir_path = frontend_dir / subdir
        dir_path.mkdir(parents=True, exist_ok=True)
        app.mount(
            f"/{subdir}", StaticFiles(directory=str(dir_path)), name=subdir
        )

    # Static files from backend asset directories
    from genmedia4commerce.config import BACKEND_ASSETS_DIR

    backend_dir = BACKEND_ASSETS_DIR
    static_mounts = {
        "/glasses/videos": backend_dir / "video_vto" / "glasses" / "videos",
        "/glasses/images/models": backend_dir
        / "image_vto"
        / "glasses"
        / "images"
        / "models",
        "/glasses/images": backend_dir / "image_vto" / "glasses" / "images",
        "/shoes/spinning/images": backend_dir
        / "spinning"
        / "r2v"
        / "shoes"
        / "images",
        "/other/images/products_r2v": backend_dir
        / "spinning"
        / "r2v"
        / "other"
        / "images",
        "/other/images/products_interpolation": backend_dir
        / "spinning"
        / "interpolation"
        / "other"
        / "images",
    }
    for mount_path, dir_path in static_mounts.items():
        if dir_path.exists():
            app.mount(
                mount_path,
                StaticFiles(directory=str(dir_path)),
                name=mount_path.strip("/").replace("/", "_"),
            )

    # SPA catch-all: serve index.html for any path not matched by API routes or static files
    from starlette.responses import FileResponse

    index_html = str(frontend_dir / "index.html")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # If the path matches an actual file in dist/, serve it
        file_path = frontend_dir / full_path
        if full_path and file_path.is_file():
            return FileResponse(str(file_path))
        # Only serve index.html for SPA routes (paths without file extensions)
        # Asset requests (.json, .js, .css, etc.) that don't exist should 404
        if "." in full_path.rsplit("/", maxsplit=1)[-1]:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(index_html)


@app.get("/health")
async def health():
    """Return health status."""
    return {"status": "healthy"}


@app.get("/api/status")
async def get_status():
    """Check platform service availability (segmentation, classifier, etc.)."""
    from fastapi.concurrency import run_in_threadpool

    status = {}

    # Check Vertex segmentation model
    def _check_segmentation():
        try:
            # Create a tiny 1x1 white PNG to probe the API
            import io

            from google.genai.types import SegmentImageSource
            from PIL import Image as PImage

            img = PImage.new("RGB", (8, 8), (255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            from google.genai import types as genai_types

            seg_source = SegmentImageSource(
                image=genai_types.Image(image_bytes=buf.getvalue())
            )
            from google import genai

            project_id = os.getenv("PROJECT_ID", "my_project")
            location = os.getenv("GLOBAL_REGION", "global")
            probe_client = genai.Client(
                vertexai=True, project=project_id, location=location
            )
            probe_client.models.segment_image(
                model="image-segmentation-001", source=seg_source
            )
            return True
        except Exception as e:
            if hasattr(e, "code") and e.code == 404:
                return False
            # Other errors (e.g., 400 for tiny image) mean the model IS enabled
            return True

    status["vertex_segmentation_enabled"] = await run_in_threadpool(
        _check_segmentation
    )
    return status


# Mount frontend LAST — the catch-all "/" mount must come after all API routes
_mount_frontend()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
