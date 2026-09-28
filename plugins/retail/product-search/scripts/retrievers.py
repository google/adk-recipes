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

"""Retrieval helpers for the retail product search agent.

Provides :func:`search_collection` for semantic product lookup via
Vertex AI Vector Search 2.0, and :func:`search` as a convenience
wrapper that reads the collection path from the environment.
"""

# pylint: disable=line-too-long
# (error messages and example URLs are intentionally long; breaking them hurts copy-paste UX.)

import re

from google.cloud import vectorsearch

from scripts.config import config

_COLLECTION_PATH_RE = re.compile(
    r"^projects/[^/\s]+/locations/[^/\s]+/collections/[^/\s]+$"
)


def _create_search_client():
    return vectorsearch.DataObjectSearchServiceClient()


def _format_result(index: int, result) -> str:
    """Format a single search result as a compact one-line string.

    Args:
        index: 1-based product index used in the formatted output.
        result: A Vector Search ``SearchDataObjectsResponse`` entry.

    Returns:
        A one-line string of the form
        ``"Product N: name, $price, by brand, rated R out of 5, <desc>"``.
    """
    data = result.data_object.data
    name = data.get("name", "Unknown")
    price = data.get("price", "N/A")
    brand = data.get("brand", "")
    rating = data.get("rating", "")
    description = data.get("description", "")

    parts = [f"{name}, ${price}"]
    if brand:
        parts.append(f"by {brand}")
    if rating:
        parts.append(f"rated {rating} out of 5")
    if description:
        parts.append(description[:80])

    return f"Product {index}: " + ", ".join(parts)


def search_collection(
    query: str,
    collection_path: str,
    top_k: int = 10,
) -> str:
    """Search a Vector Search 2.0 Collection using semantic search.

    Args:
        query: The search query text.
        collection_path: Full resource path of the collection.
        top_k: Number of results to return.

    Returns:
        Formatted string containing relevant document content.
    """
    client = _create_search_client()

    request = vectorsearch.SearchDataObjectsRequest(
        parent=collection_path,
        semantic_search=vectorsearch.SemanticSearch(
            search_text=query,
            search_field="text_embedding",
            task_type="QUESTION_ANSWERING",
            top_k=top_k,
            output_fields=vectorsearch.OutputFields(
                data_fields=[
                    "product_id",
                    "name",
                    "price",
                    "description",
                    "category",
                    "brand",
                    "rating",
                    "stock",
                ]
            ),
        ),
    )

    results = client.search_data_objects(request)

    formatted_parts = [
        _format_result(i + 1, result) for i, result in enumerate(results)
    ]

    if not formatted_parts:
        return "No matching products found. Suggest broadening the search."

    return (
        "Found "
        + str(len(formatted_parts))
        + " products. "
        + ". ".join(formatted_parts)
    )


def search(query: str, top_k: int = 5) -> str:
    """Run the SKILL.md smoke-test query against the configured collection.

    Reads the collection path from the ``VECTOR_SEARCH_COLLECTION`` env var,
    which is what ``scripts/setup.py`` prints at the end of a successful run.

    Args:
        query: The product search query in natural language.
        top_k: Maximum number of matching products to return.

    Returns:
        A formatted string with the matching products, or a "no matches"
        message if the collection returned nothing.

    Raises:
        RuntimeError: If ``VECTOR_SEARCH_COLLECTION`` is not set, or is set
            to a malformed path (typically with an embedded newline from a
            wrapped multi-line shell paste).
    """
    raw = config.VECTOR_SEARCH_COLLECTION
    if not raw:
        raise RuntimeError(
            "VECTOR_SEARCH_COLLECTION env var is not set. Set it to the collection "
            "path printed by scripts/setup.py, e.g. "
            "projects/<PROJECT>/locations/us-central1/collections/retail-skill-products-collection"
        )
    # Strip whitespace -- a newline embedded mid-path (from a wrapped multi-line
    # shell paste) silently causes the SDK to 501 from the wrong endpoint.
    collection_path = raw.strip()
    if not _COLLECTION_PATH_RE.match(collection_path):
        raise RuntimeError(
            "VECTOR_SEARCH_COLLECTION is malformed -- expected "
            "'projects/<project>/locations/<region>/collections/<id>' with no whitespace. "
            f"Got: {raw!r}. "
            "Tip: type the export on a single line, e.g. "
            "export VECTOR_SEARCH_COLLECTION="
            '"projects/$GOOGLE_CLOUD_PROJECT/locations/us-central1/'
            'collections/retail-skill-products-collection"'
        )
    return search_collection(query, collection_path, top_k=top_k)
