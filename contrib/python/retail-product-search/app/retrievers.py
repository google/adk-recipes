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
Vector Search 2.0 on Gemini Enterprise Agent Platform, and :func:`search`
as a convenience wrapper that reads the collection path from the environment.
"""

import re

from google.cloud import vectorsearch

from app.config import config

_COLLECTION_PATH_RE = re.compile(
    r"^projects/[^/\s]+/locations/[^/\s]+/collections/[^/\s]+$"
)


def _create_search_client():
    return vectorsearch.DataObjectSearchServiceClient()


def _format_result(index: int, result) -> str:
    data = dict(result.data_object.data)
    parts = [f"Product {index + 1}:"]
    if data.get("name"):
        parts.append(f"{data['name']}")
    if data.get("price") is not None:
        parts.append(f"${data['price']}")
    if data.get("brand"):
        parts.append(f"by {data['brand']}")
    if data.get("category"):
        parts.append(f"({data['category']})")
    if data.get("rating") is not None:
        parts.append(f"rated {data['rating']}/5")
    if data.get("stock") is not None:
        parts.append(f"({int(data['stock'])} in stock)")
    if data.get("description"):
        parts.append(f"- {data['description'][:200]}")
    return " ".join(parts)


def search_collection(
    query: str,
    collection_path: str,
    top_k: int = 10,
) -> list[dict]:
    """Search a Vector Search 2.0 collection using semantic similarity."""
    search_client = _create_search_client()
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
                ],
            ),
        ),
    )

    try:
        response = search_client.search_data_objects(request=request)
        results = list(response.results)
    except Exception as e:
        return [{"content": f"Search error: {e}", "score": 0.0}]

    if not results:
        return []

    formatted = []
    for i, result in enumerate(results):
        data = dict(result.data_object.data)
        formatted.append(
            {
                "content": _format_result(i, result),
                "product_id": data.get("product_id", ""),
                "name": data.get("name", ""),
                "price": data.get("price"),
                "category": data.get("category", ""),
                "brand": data.get("brand", ""),
            }
        )

    return formatted


def search(query: str, top_k: int = 5) -> list[dict]:
    """Search products using the collection from VECTOR_SEARCH_COLLECTION env var."""
    raw = config.VECTOR_SEARCH_COLLECTION
    collection_path = raw.strip()
    if not collection_path:
        raise ValueError(
            "VECTOR_SEARCH_COLLECTION environment variable is required. "
            "Example: projects/my-project/locations/us-central1/collections/my-products"
        )
    if not _COLLECTION_PATH_RE.match(collection_path):
        raise ValueError(
            f"VECTOR_SEARCH_COLLECTION is malformed: {raw!r}. "
            "Expected 'projects/<project>/locations/<region>/collections/<id>' "
            "with no whitespace or newlines."
        )
    return search_collection(
        query=query,
        collection_path=collection_path,
        top_k=top_k,
    )
