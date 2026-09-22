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
import logging
import os

import google.auth
from fastapi import FastAPI, HTTPException, status
from google.adk.cli.fast_api import get_fast_api_app
from pydantic import BaseModel, Field

from app.config import DEFAULT_TOP_K, MAX_TOP_K
from app.mcp_server import generate_answer
from app.mcp_server import server as mcp_server
from app.vector_search import search_knowledge_base

try:
    _, project_id = google.auth.default()
except Exception:
    project_id = None

logger = logging.getLogger(__name__)

allow_origins = (
    os.getenv("ALLOW_ORIGINS").split(",")
    if os.getenv("ALLOW_ORIGINS")
    else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=project_id is not None and not os.getenv("INTEGRATION_TEST"),
)
app.title = "multiformat-hybrid-rag"
app.description = "API for interacting with the Agent multiformat-hybrid-rag"


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    conversation_summary: str = ""
    generative_answer: bool = True


@app.post("/api/search")
def search(req: SearchRequest) -> dict:
    """REST endpoint for knowledge base search.

    With generative_answer=True (default), returns an LLM-generated answer.
    With generative_answer=False, returns raw retrieved documents.
    """
    try:
        context = search_knowledge_base(
            query=req.query,
            top_k=req.top_k,
            generative_answer=req.generative_answer,
        )
        if not req.generative_answer:
            return {"result": context}

        if context.startswith("No relevant documents"):
            return {"result": context}

        answer = generate_answer(context, req.conversation_summary, req.query)
        return {"result": answer}
    except Exception:
        # Log the detail server-side; return something generic. The previous
        # form handed the exception class and message to the caller, which
        # leaks internals (resource paths, backend errors) and reports a
        # failure as HTTP 200.
        logger.exception("search request failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed. See server logs.",
        ) from None


# --- Mount MCP server into FastAPI (same port, externally reachable) ---
# Don't pass mount_path here — FastAPI's mount() already prepends /mcp.
# Passing it would double-prefix the SSE messages URL to /mcp/mcp/messages/...
app.mount("/mcp", mcp_server.sse_app())

# No MCP_SERVER_URL default is set here. app/agent.py reads that variable at
# import time and is imported via app/__init__.py before this module finishes
# executing, so any write at this point would have no reader -- the agent has
# already chosen its transport. The deployed service gets the URL injected as
# a real Cloud Run environment variable (infra/terraform/dev/service.tf);
# locally it stays empty and the agent falls back to a stdio subprocess,
# which is what `adk web` wants anyway.

# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
