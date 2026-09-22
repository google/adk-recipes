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
import argparse
import asyncio
import logging
import os
import re
from typing import Annotated

from google import genai
from google.genai import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from app.config import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    MCP_TOOL_MODEL,
    MODEL_LOCATION,
    get_project_id,
    init_vertex,
)
from app.vector_search import search_knowledge_base

logger = logging.getLogger(__name__)

# Cached at first use rather than built at import. Constructing a Client
# resolves credentials and vertexai.init() reaches for ADC, so doing either
# at module scope would make importing this module a network operation.
# Mirrors the lazy-singleton pattern in app/vector_search.py.
_genai_client: genai.Client | None = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        init_vertex()
        _genai_client = genai.Client(
            vertexai=True,
            project=get_project_id(),
            location=MODEL_LOCATION,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    initial_delay=1.0,
                    attempts=3,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
            ),
        )
    return _genai_client


# Low but not zero: answers must stay faithful to the retrieved documents,
# while leaving enough freedom to phrase them naturally.
ANSWER_TEMPERATURE = 0.2

_SYSTEM_PROMPT = """\
You are a knowledge base assistant. Answer questions based exclusively on the \
provided documents.

Rules:
- Base your answer ONLY on the documents provided below. Do not use external knowledge.
- If the documents do not contain enough information to answer, state this clearly.
- When multiple documents cover the same topic from different time periods, \
prefer the most recent information. Do not mention outdated offers, prices, or \
procedures if a newer version is available in the documents.
- Cite the source document name when available.
- Reply in the same language as the question.
- Be concise and direct."""

# DNS rebinding protection only supports exact hosts or :* port wildcards
# (no subdomain wildcards). Cloud Run URLs change between deploys, so we
# disable the host check here — Cloud Run IAM (--no-allow-unauthenticated)
# is the real security boundary, not the Host header.
server = FastMCP(
    "multiformat-hybrid-rag",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


def _normalize_whitespace(text: str) -> str:
    text = re.sub(r"[^\S\n]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def generate_answer(
    context: str, conversation_summary: str, question: str
) -> str:
    """Generate an answer grounded in the retrieved context.

    Public because both entry points use it: the ask_knowledge_base tool
    below and the /api/search endpoint in app/fast_api_app.py. It stays in
    this module because it shares the lazily constructed GenAI client and
    the system prompt with the tool.

    Blocking -- callers on an event loop must offload it (see
    ask_knowledge_base).
    """
    context = _normalize_whitespace(context)
    resp = _get_genai_client().models.generate_content(
        model=MCP_TOOL_MODEL,
        contents=(
            f"{context}\n\n"
            f"## Conversation so far:\n{conversation_summary}\n\n"
            f"## Question:\n{question}"
        ),
        config={
            "temperature": ANSWER_TEMPERATURE,
            "system_instruction": _SYSTEM_PROMPT,
        },
    )
    return resp.text or ""


@server.tool()
async def ask_knowledge_base(
    conversation_summary: str,
    question: str,
    top_k: Annotated[int, Field(ge=1, le=MAX_TOP_K)] = DEFAULT_TOP_K,
    generative_answer: bool = True,
) -> str:
    """Ask the knowledge base a question.

    Searches the knowledge base for relevant documents. When generative_answer
    is True (default), uses Gemini to generate a direct answer grounded in the
    retrieved content. When False, returns the raw retrieved documents.

    Args:
        conversation_summary: Summary of the conversation so far, for context.
        question: The user's question to answer.
        top_k: Number of documents to retrieve for context.
        generative_answer: If True, generate an answer with Gemini. If False,
            return the raw retrieved documents.

    Returns:
        A direct answer or raw document content from the knowledge base.
    """
    # search_knowledge_base and generate_answer are both synchronous and
    # both block for seconds -- a gRPC round trip to Vector Search and a
    # Gemini generate_content call. This coroutine runs on the same event
    # loop as the FastAPI app that mounts the MCP server (see
    # app/fast_api_app.py), so calling them inline would stall every other
    # request on the instance. FastMCP does not offload sync tools to a
    # worker thread -- it awaits async ones and calls sync ones directly --
    # so the hand-off has to happen here.
    try:
        context = await asyncio.to_thread(
            search_knowledge_base,
            query=question,
            top_k=top_k,
            generative_answer=generative_answer,
        )
    except Exception:
        # Detail stays in the server log. The return value crosses the MCP
        # boundary to the caller, and backend exceptions here carry full
        # Vector Search resource paths and project IDs.
        logger.exception("Knowledge base search failed")
        return (
            "The knowledge base search failed. Please try again; if the "
            "problem persists, contact the administrator."
        )

    if context.startswith("No relevant documents"):
        return "I couldn't find any relevant documents in the knowledge base to answer this question."

    if not generative_answer:
        return context

    try:
        return await asyncio.to_thread(
            generate_answer, context, conversation_summary, question
        )
    except Exception:
        logger.exception("Answer generation failed")
        return (
            "I found relevant documents but could not generate an answer. "
            "Please try again; if the problem persists, contact the "
            "administrator."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio"
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("MCP_SERVER_PORT", "8081"))
    )
    args = parser.parse_args()

    if args.transport == "sse":
        server.settings.port = args.port
    server.run(transport=args.transport)
