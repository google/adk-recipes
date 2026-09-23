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

import logging
import os
import sys

from google import genai
from google.cloud import storage

# ==============================================================================
# Configuration Logic (Direct Env Reading)
# ==============================================================================

# Configure logging
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )


def get_logger(name: str):
    """Returns a standard Python logger."""
    return logging.getLogger(name)


log = get_logger("config")

# Exported Config Variables (Directly read from environment)
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
DEFAULT_REGIONAL_LOCATION = "us-central1"


def resolve_regional_location(location: str | None) -> str:
    """Translates multi-region endpoint locations ('global' or 'us') to a regional GCP location."""
    if not location or location.lower() in ("global", "us"):
        return DEFAULT_REGIONAL_LOCATION
    return location


ROOT_MODEL = os.getenv("GEMINI_MODEL_NAME")
IMAGE_GENERATION_MODEL = os.getenv("IMAGE_GENERATION_MODEL")
PROJECT_NUMBER = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER")
DATASTORE_ID = os.getenv("DATASTORE_ID")
DEFAULT_TEMPLATE_URI = os.getenv("DEFAULT_TEMPLATE_URI")
ENABLE_RAG = (os.getenv("ENABLE_RAG") or "").lower() == "true"
ENABLE_DEEP_RESEARCH = (
    os.getenv("ENABLE_DEEP_RESEARCH") or ""
).lower() == "true"
MODEL_ARMOR_TEMPLATE_ID = os.getenv("MODEL_ARMOR_TEMPLATE_ID")

# Format GCS Bucket Name
GCS_BUCKET_NAME = os.getenv("GCP_STAGING_BUCKET")
if not GCS_BUCKET_NAME and GOOGLE_CLOUD_PROJECT:
    GCS_BUCKET_NAME = f"{GOOGLE_CLOUD_PROJECT}-staging-bucket"


if GCS_BUCKET_NAME:
    # Safely strip prefix and trailing slashes
    GCS_BUCKET_NAME = GCS_BUCKET_NAME.replace("gs://", "").strip("/")

    # Ensure we only take the root bucket name if a path was provided
    GCS_BUCKET_NAME = GCS_BUCKET_NAME.split("/")[0]


PRESENTATION_SPEC_ARTIFACT = "presentation_spec.json"
RESEARCH_SUMMARY_ARTIFACT = "research_summary.txt"

# Global genai client, initialized via function
_genai_client = None

# ==============================================================================
# Client Initialization and Logging Utilities
# ==============================================================================


def initialize_genai_client():
    """Initializes and returns the global Vertex AI GenAI client."""
    global _genai_client
    if _genai_client is None:
        try:
            _genai_client = genai.Client(
                vertexai=True,
                project=GOOGLE_CLOUD_PROJECT,
                location=GOOGLE_CLOUD_LOCATION,
            )
            log.info(
                f"Vertex AI client initialized for project '{GOOGLE_CLOUD_PROJECT}' in location '{GOOGLE_CLOUD_LOCATION}'."
            )
        except Exception as e:
            log.error(f"CRITICAL: Failed to initialize Vertex AI client: {e}")
            _genai_client = None
    return _genai_client


def get_gcs_client():
    """Initializes and returns a GCS client with robust project detection."""
    try:
        # If GOOGLE_CLOUD_PROJECT is provided, use it; otherwise, let the SDK auto-detect
        if GOOGLE_CLOUD_PROJECT:
            client = storage.Client(project=GOOGLE_CLOUD_PROJECT)
        else:
            client = storage.Client()
        return client
    except Exception as e:
        get_logger("get_gcs_client").error(
            f"Failed to initialize GCS client: {e}"
        )
        get_logger("get_gcs_client").warning(
            "Please ensure you are authenticated (e.g., `gcloud auth application-default login`)"
        )
        return None
