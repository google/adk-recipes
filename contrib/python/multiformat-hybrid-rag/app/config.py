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

"""Shared configuration for the serving layer (agent, MCP, REST API).

Single source of truth for env-var-driven defaults so agent.py,
mcp_server.py, and fast_api_app.py don't redefine the same constants.
"""

import functools
import os

# Environment is bootstrapped in app/__init__.py (load_dotenv), which runs
# before this module is imported. Nothing here may perform I/O at import
# time: the recipe standards forbid module-import-time side effects, and
# the runnability test imports this package with no credentials present.


# Region for data-plane resources: GCS, BigQuery, Vector Search, Cloud Run.
def env_or(name: str, default: str) -> str:
    """Read an env var, treating an empty value as unset.

    .env.example is loaded as a runtime fallback (see app/__init__.py) and
    must declare every variable the recipe reads -- including ones whose
    real default is *derived* at runtime, which are therefore declared
    empty. Plain os.getenv(name, default) returns "" for those rather than
    the default, because the key IS present. That silently produced an
    empty Vector Search collection path and a 404 at query time.
    """
    return os.getenv(name) or default


LOCATION = env_or("GOOGLE_CLOUD_LOCATION", "us-central1")

# Endpoint that serves the Gemini models. Deliberately separate from
# LOCATION: the gemini-3.x family is published only on the `global`
# endpoint and returns 404 NOT_FOUND from a regional one. Keep this at
# "global" unless a model is verified as available in your region.
MODEL_LOCATION = env_or("GOOGLE_CLOUD_LOCATION_MODELS", "global")


@functools.cache
def get_project_id() -> str:
    """Resolve the GCP project, preferring explicit config over ADC.

    Deferred rather than module-scope: google.auth.default() performs
    credential discovery and can reach the metadata server.
    """
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if project:
        return project

    import google.auth

    _, project = google.auth.default()
    return project or ""


@functools.cache
def init_vertex() -> None:
    """Initialise the Vertex AI SDK exactly once, on first use."""
    import vertexai

    vertexai.init(project=get_project_id(), location=LOCATION)


# --- Vector Search collections -------------------------------------------
SEMANTIC_WEIGHT = float(env_or("VS_SEMANTIC_WEIGHT", "0.7"))
VS_COLLECTION_ID = env_or(
    "VS_COLLECTION_ID", "multiformat-hybrid-rag-collection"
)
VS_DOCUMENTS_COLLECTION_ID = env_or(
    "VS_DOCUMENTS_COLLECTION_ID", "multiformat-hybrid-rag-documents"
)


def _collection_path(collection_id: str) -> str:
    """Build a Vector Search collection resource path.

    Not cached itself -- the two callers below are, and they are the only
    ones. get_project_id() is where the expensive part lives.
    """
    return (
        f"projects/{get_project_id()}/locations/{LOCATION}"
        f"/collections/{collection_id}"
    )


@functools.cache
def get_collection_path() -> str:
    """Full resource path of the chunks collection."""
    return env_or(
        "VECTOR_SEARCH_COLLECTION", _collection_path(VS_COLLECTION_ID)
    )


@functools.cache
def get_documents_collection_path() -> str:
    """Full resource path of the documents-by-file_id KV collection."""
    return env_or(
        "VECTOR_SEARCH_DOCUMENTS_COLLECTION",
        _collection_path(VS_DOCUMENTS_COLLECTION_ID),
    )


# --- Gemini models -------------------------------------------------------
AGENT_MODEL = env_or("MODEL_NAME", "gemini-3.7-flash")
MCP_TOOL_MODEL = env_or("MODEL_NAME_MCP_TOOL", "gemini-3.5-flash-lite")

# --- Answer generation ---------------------------------------------------
CONTEXT_WINDOW = int(env_or("MCP_CONTEXT_WINDOW", "10000"))

# Documents returned when the caller does not say. Shared so the REST
# endpoint, the MCP tool and search_collection cannot drift apart.
DEFAULT_TOP_K = 10

# Upper bound on the documents a single request may ask for. Both entry
# points are externally reachable -- the REST endpoint and the MCP server
# mounted at /mcp -- and top_k fans out into a Vector Search request per
# retriever and then into the Gemini prompt, so an unbounded value lets a
# caller exhaust backend resources.
MAX_TOP_K = 100
