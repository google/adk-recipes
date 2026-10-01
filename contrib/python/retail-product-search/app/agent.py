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

"""ADK agent definition for the retail product search agent.

Exposes a :data:`root_agent` (and :data:`app`) that uses Vector Search 2.0
on Gemini Enterprise Agent Platform for semantic product retrieval via the
:func:`retrieve_docs` tool.
"""

import os
import re

import google
import vertexai
from google.adk import agents, apps, models

from app.config import config
from app.retrievers import search_collection

_COLLECTION_PATH_RE = re.compile(
    r"^projects/[^/\s]+/locations/[^/\s]+/collections/[^/\s]+$"
)

_resolved_project_id: str | None = None


def _resolve_project_id() -> str:
    """Resolve the effective GCP project id and init Gemini Enterprise Agent Platform on first call."""
    global _resolved_project_id
    if _resolved_project_id is not None:
        return _resolved_project_id
    project_id: str | None = config.GOOGLE_CLOUD_PROJECT or None
    if not project_id:
        _, adc_default = google.auth.default()
        project_id = adc_default
    if not project_id:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set and ADC has no default project. "
            "Copy .env.example to .env and set GOOGLE_CLOUD_PROJECT, or run "
            "`gcloud auth application-default set-quota-project <PROJECT>`."
        )
    if "GOOGLE_CLOUD_PROJECT" not in os.environ:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    if "GOOGLE_CLOUD_LOCATION" not in os.environ:
        os.environ["GOOGLE_CLOUD_LOCATION"] = config.GOOGLE_CLOUD_LOCATION
    vertexai.init(project=project_id, location=config.GOOGLE_CLOUD_LOCATION)
    _resolved_project_id = project_id
    return project_id


def _get_vector_search_collection() -> str:
    """Return the Vector Search collection resource path."""
    raw = config.VECTOR_SEARCH_COLLECTION
    if raw and raw.strip():
        candidate = raw.strip()
        if not _COLLECTION_PATH_RE.match(candidate):
            raise ValueError(
                f"VECTOR_SEARCH_COLLECTION is malformed: {raw!r}. "
                "Expected 'projects/<project>/locations/<region>/collections/<id>' "
                "with no whitespace or newlines."
            )
        return candidate
    project_id = _resolve_project_id()
    vs_location = config.VECTOR_SEARCH_LOCATION
    return (
        f"projects/{project_id}/locations/{vs_location}"
        "/collections/retail-skill-products-collection"
    )


def retrieve_docs(query: str) -> str:
    """Retrieve relevant products from the catalog based on a search query."""
    try:
        collection_path = _get_vector_search_collection()
        results = search_collection(
            query=query,
            collection_path=collection_path,
            top_k=10,
        )
        if not results:
            return "No products found matching your query."
        formatted = [
            result.get("content", str(result))
            if isinstance(result, dict)
            else str(result)
            for result in results
        ]
        return "\n\n".join(formatted)
    except Exception as e:
        return f"Error searching products: {e}"


INSTRUCTION = """You are a helpful product search assistant for an e-commerce store.

Your role is to help customers find products by searching the catalog and providing clear, helpful recommendations.

Guidelines:
- Always search the product catalog using the retrieve_docs tool before answering product questions.
- Present results clearly with product name, price, brand, and a brief description.
- When multiple products match, highlight the differences to help the customer choose.
- If a query is vague (e.g., "I need a gift"), ask clarifying questions about category, budget, or recipient preferences while also showing a few popular options.
- Never fabricate product details, prices, or availability. Only share information returned by the search tool.
- If no products match, suggest broadening the search or trying related terms.
- Keep responses concise and focused on helping the customer make a decision."""

root_agent = agents.Agent(
    name="product_search_agent",
    model=models.Gemini(
        model=config.GEMINI_MODEL,
    ),
    description="Retail product search assistant with semantic catalog lookup",
    instruction=INSTRUCTION,
    tools=[retrieve_docs],
)

app = apps.App(
    name="app",
    root_agent=root_agent,
)
