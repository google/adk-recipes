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

"""RAG Template configuration.

Handles environment setup, authentication, and a single Gemini client.
Use `from src.utils.config import config` and `get_genai_client()`.

Importing this module reads .env / .env.example and builds the config
dataclass. It performs no network I/O: credential discovery is deferred to
bootstrap_auth(), which callers invoke when they actually need the SDK.
"""

import functools
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent

# ---------------------------------------------------------------------------
# Load environment variables
#
# Real values come from .env (gitignored) or the ambient environment — on
# Cloud Run the latter, injected by Terraform. .env.example is the committed
# fallback so the unit tests import cleanly with no .env present; see the
# repo AGENTS.md rule that tests may rely on .env.example but not on .env.
# override=False throughout, so a real environment variable always wins.
# ---------------------------------------------------------------------------
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
load_dotenv(dotenv_path=PROJECT_ROOT / ".env.example", override=False)


# ---------------------------------------------------------------------------
# Authentication
#
# Deliberately NOT executed at import time: google.auth.default() performs
# credential discovery (and can reach the metadata server), which would make
# importing this module a network operation. Callers that need the SDK
# configured invoke bootstrap_auth() explicitly.
# ---------------------------------------------------------------------------
def bootstrap_auth() -> None:
    """Populate the GOOGLE_* variables the GenAI SDK expects. Idempotent."""
    if os.getenv("GOOGLE_API_KEY"):
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")
        return

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        try:
            import google.auth

            _, project_id = google.auth.default()
        except Exception:
            project_id = ""
            logger.warning("No GOOGLE_CLOUD_PROJECT set and ADC not available")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------
def _get(
    key: str, default: str | None = None, allow_empty: bool = False
) -> str:
    """Get a config value from environment, raising if missing and no default.

    An empty value counts as unset unless allow_empty is True. .env.example is
    loaded as a runtime fallback and necessarily declares some keys empty, so
    treating "" as a real value would both shadow the defaults below and let a
    blanked *required* key slip through as "" instead of raising. Mirrors
    app.config.env_or.
    """
    val = os.environ.get(key)
    if val == "" and not allow_empty:
        val = None
    if val is None:
        val = default
    if val is None:
        raise ValueError(f"Missing required config: {key}")
    return val


@dataclass(frozen=True)
class Config:
    project_id: str
    region: str
    gcs_bucket: str
    gcs_prefix: str
    bq_dataset: str
    bq_object_table: str
    bq_preprocessed_table: str
    bq_chunks_table: str
    bq_gcs_connection_id: str
    vs_collection_id: str
    vs_documents_collection_id: str
    vs_embedding_model: str
    vs_embedding_dims: int
    vs_batch_size: int
    agent_gemini_model: str
    markdown_converter_gemini_model: str
    contextual_chunking_gemini_model: str
    relevance_gemini_model: str
    vs_semantic_weight: float
    chunk_size: int
    chunk_overlap: int

    @property
    def collection_path(self) -> str:
        return f"projects/{self.project_id}/locations/{self.region}/collections/{self.vs_collection_id}"

    @property
    def documents_collection_path(self) -> str:
        return f"projects/{self.project_id}/locations/{self.region}/collections/{self.vs_documents_collection_id}"

    @property
    def fq_object_table(self) -> str:
        return f"{self.project_id}.{self.bq_dataset}.{self.bq_object_table}"

    @property
    def fq_preprocessed_table(self) -> str:
        return (
            f"{self.project_id}.{self.bq_dataset}.{self.bq_preprocessed_table}"
        )

    @property
    def fq_chunks_table(self) -> str:
        return f"{self.project_id}.{self.bq_dataset}.{self.bq_chunks_table}"

    @property
    def gcs_uri_prefix(self) -> str:
        return f"gs://{self.gcs_bucket}/{self.gcs_prefix}"

    @property
    def bq_connection_path(self) -> str:
        return f"projects/{self.project_id}/locations/{self.region}/connections/{self.bq_gcs_connection_id}"


# Singleton config — loaded once at import time
config = Config(
    project_id=_get("GOOGLE_CLOUD_PROJECT"),
    region=_get("GOOGLE_CLOUD_LOCATION"),
    gcs_bucket=_get("GCS_BUCKET"),
    # allow_empty: an empty prefix legitimately means "the whole bucket".
    gcs_prefix=_get("GCS_PREFIX", "documents/", allow_empty=True),
    bq_dataset=_get("BQ_DATASET"),
    bq_object_table=_get("BQ_OBJECT_TABLE"),
    bq_preprocessed_table=_get("BQ_PREPROCESSED_TABLE"),
    bq_chunks_table=_get("BQ_CHUNKS_TABLE"),
    bq_gcs_connection_id=_get("BQ_GCS_CONNECTION_ID"),
    vs_collection_id=_get("VS_COLLECTION_ID"),
    vs_documents_collection_id=_get("VS_DOCUMENTS_COLLECTION_ID"),
    vs_embedding_model=_get("VS_EMBEDDING_MODEL", "gemini-embedding-001"),
    vs_embedding_dims=int(_get("VS_EMBEDDING_DIMS", "3072")),
    vs_batch_size=int(_get("VS_BATCH_SIZE", "250")),
    agent_gemini_model=_get("MODEL_NAME", "gemini-3.7-flash"),
    markdown_converter_gemini_model=_get(
        "MARKDOWN_CONVERTER_GEMINI_MODEL", "gemini-3.7-flash"
    ),
    contextual_chunking_gemini_model=_get(
        "CONTEXTUAL_CHUNKING_GEMINI_MODEL", "gemini-3.5-flash-lite"
    ),
    relevance_gemini_model=_get(
        "RELEVANCE_GEMINI_MODEL", "gemini-3.5-flash-lite"
    ),
    vs_semantic_weight=float(_get("VS_SEMANTIC_WEIGHT", "0.7")),
    chunk_size=int(_get("CHUNK_SIZE", "800")),
    chunk_overlap=int(_get("CHUNK_OVERLAP", "50")),
)


# ---------------------------------------------------------------------------
# Shared Gemini client (single global endpoint — works for all models)
#
# Built on first use rather than at import. Constructing a Client resolves
# credentials, so doing it at module scope made `import src.utils.config` a
# credential-discovery operation — the import-time side effect the recipe
# standards prohibit.
# ---------------------------------------------------------------------------
@functools.cache
def get_genai_client():
    """Return the shared Vertex-backed Gemini client, or None if unavailable."""
    from google import genai
    from google.genai import types

    bootstrap_auth()
    try:
        return genai.Client(
            vertexai=True,
            project=config.project_id,
            location="global",
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    initial_delay=1.0,
                    attempts=5,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
            ),
        )
    except Exception as e:
        logger.warning("Gemini client not available: %s", e)
        return None
