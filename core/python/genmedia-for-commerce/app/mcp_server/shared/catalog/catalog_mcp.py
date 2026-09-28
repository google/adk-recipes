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

"""MCP tool wrapper for catalog search."""

import asyncio
import logging

from workflows.shared.vector_search import search

logger = logging.getLogger(__name__)


async def run_catalog_search(
    query: str,
    k: int = 12,
    audience: str | None = None,
) -> dict:
    """Search the product catalog for items matching a text query.

    Uses vector similarity search to find products matching the description.

    Args:
        query: Text description of what to search for
            (e.g. "red casual dress", "blue running shoes").
        k: Number of results to return. Default: 12.
        audience: Optional filter for target audience. Accepted values:
            "women", "men", "unisex", or a comma-separated combination
            (e.g. "men,unisex"). If not specified, searches all audiences.

    Returns:
        Dictionary with 'results' list containing matched products
        with description, image URL, category, color, style, audience, and score.

    """
    filters = None
    if audience:
        audiences = [a.strip() for a in audience.split(",")]
        if "unisex" not in audiences:
            audiences.append("unisex")
        filters = {"audience": audiences}

    logger.info(
        f"[MCP catalog_search] Query: {query}, k={k}, filters={filters}"
    )

    loop = asyncio.get_event_loop()

    try:
        results = await loop.run_in_executor(
            None, lambda: search(query, k=k, filters=filters)
        )
    except Exception as e:
        logger.error(f"[MCP catalog_search] Search failed: {e}")
        return {"results": [], "error": str(e)}

    def _format_results(results):
        return [item.get("data", {}) for item in results]

    logger.info(f"[MCP catalog_search] Complete. {len(results)} results")
    return {"results": _format_results(results)}
