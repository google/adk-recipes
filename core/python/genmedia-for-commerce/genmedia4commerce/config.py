# Copyright 2025 Google LLC
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

"""GenMedia for Commerce configuration.

Handles authentication, environment setup, and asset downloads.
Imported by agent.py at startup.
"""

import hashlib
import io
import logging
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables from config.env
env_path = Path(__file__).parent / "config.env"
load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# Always resolve project_id up front so downstream references (e.g. the
# MEDIA_BUCKET below) never NameError, regardless of which auth mode is
# selected. Both PROJECT_ID (recipe-specific) and GOOGLE_CLOUD_PROJECT
# (standard GCP env var) are accepted.
project_id = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "")

if os.getenv("GOOGLE_API_KEY"):
    # AI Studio mode: set explicitly so a stale env value cannot silently
    # override the detected auth mode.
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    # Vertex AI mode: Use PROJECT_ID from env, fall back to ADC
    if not project_id:
        try:
            import google.auth

            _, project_id = google.auth.default()
        except Exception:
            project_id = ""
            logger.warning("No PROJECT_ID set and ADC not available")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv("GLOBAL_REGION", "global")
    # Vertex AI mode: always set explicitly (computed, not user-configurable).
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

# GCS bucket computed from seed hash (same as catalogue in vector_search.py)
_ASSET_SEED = "genmedia_for_commerce_generated_fashion_images"
_ASSET_BUCKET = hashlib.sha256(_ASSET_SEED.encode()).hexdigest()[:63]
_ASSET_PREFIX = "genmedia_for_commerce_repo_assets"
_BACKEND_ASSETS_TAR = "backend_assets.tar.gz"
_FRONTEND_ASSETS_TAR = "frontend_assets.tar.gz"

# Local assets directory (project root / assets)
ASSETS_DIR = Path(__file__).parent.parent / "assets"
BACKEND_ASSETS_DIR = ASSETS_DIR / "backend_assets"
FRONTEND_ASSETS_DIR = ASSETS_DIR / "frontend_assets"

# Files that must exist to consider each asset set complete.
_BACKEND_REQUIRED = [
    BACKEND_ASSETS_DIR / "catalogue" / "metadata.parquet",
]
_FRONTEND_REQUIRED: list[Path] = []


def _pull_tar(
    tar_name: str, target_dir: Path, required_files: list[Path] | None = None
):
    """Download and extract a single asset tar from GCS if not present locally."""
    if target_dir.exists() and any(target_dir.iterdir()):
        missing = [f for f in (required_files or []) if not f.exists()]
        if not missing:
            logger.info(f"{target_dir.name} already present, skipping download")
            return
        logger.info(
            f"{target_dir.name} incomplete (missing {len(missing)} files), re-downloading"
        )

    logger.info(f"Downloading {tar_name} from GCS...")
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(_ASSET_BUCKET)
        blob = bucket.blob(f"{_ASSET_PREFIX}/{tar_name}")

        if not blob.exists():
            logger.warning(
                f"{tar_name} not found in GCS — run 'make push-assets' to upload"
            )
            return

        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        tar_bytes = blob.download_as_bytes()
        buf = io.BytesIO(tar_bytes)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            tar.extractall(path=str(ASSETS_DIR), filter="data")

        size_mb = len(tar_bytes) / (1024 * 1024)
        logger.info(f"Extracted {tar_name} ({size_mb:.1f} MB) to {target_dir}")
    except Exception as e:
        logger.warning(f"Could not download {tar_name}: {e}")


def pull_assets():
    """Download all assets from GCS if not present locally.

    Called automatically at import time. No-op when assets already exist
    (e.g. local dev with assets on disk, or Docker with COPY assets/).
    """
    if os.getenv("INTEGRATION_TEST"):
        return
    _pull_tar(_BACKEND_ASSETS_TAR, BACKEND_ASSETS_DIR, _BACKEND_REQUIRED)
    _pull_tar(_FRONTEND_ASSETS_TAR, FRONTEND_ASSETS_DIR, _FRONTEND_REQUIRED)


# ---------------------------------------------------------------------------
# Media storage bucket (user uploads & AI outputs)
# ---------------------------------------------------------------------------


# Warn (rather than raise) when project_id is missing so tests and offline
# tooling can still import this module without live GCP credentials.
# Runtime code paths that actually touch GCS/Vertex will surface a clear
# error there when a real project is required.
if not project_id:
    logger.warning(
        "PROJECT_ID is not set; MEDIA_BUCKET will be unusable until it is "
        "configured via environment variables (PROJECT_ID or "
        "GOOGLE_CLOUD_PROJECT)."
    )
MEDIA_BUCKET = f"{project_id}-genmedia-for-commerce-media-payloads"

# ---------------------------------------------------------------------------
# Shared Gemini client (reused across agent utilities)
# ---------------------------------------------------------------------------
from google import genai  # noqa: E402

try:
    genai_client = genai.Client(
        vertexai=True,
        project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
    )
except Exception as e:
    genai_client = None
    logger.warning(f"Gemini client not available: {e}")

# Test Vertex AI API availability at startup
if genai_client is not None and not os.getenv("INTEGRATION_TEST"):
    try:
        import google.auth
        import google.auth.transport.requests

        from genmedia4commerce.agent_utils import test_vertex_connection

        _creds, _ = google.auth.default()
        _creds.refresh(google.auth.transport.requests.Request())
        test_vertex_connection(
            os.getenv("GOOGLE_CLOUD_PROJECT", ""), _creds.token
        )
    except Exception as e:
        logger.warning(f"Vertex AI API availability check skipped: {e}")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class GenMediaConfig:
    """Configuration for GenMedia agent.

    Attributes:
        agent_model: Model for the router agent.
        mcp_server_url: URL of the MCP server (SSE endpoint).

    """

    agent_model: str = os.getenv("MODEL_NAME_GENERATED_1", "gemini-3.6-flash")
    mcp_server_url: str = os.getenv(
        "MCP_SERVER_URL", "http://localhost:8081/sse"
    )


config = GenMediaConfig()

# ---------------------------------------------------------------------------
# Auto-download assets at import time (no-op if already on disk)
# ---------------------------------------------------------------------------
pull_assets()
