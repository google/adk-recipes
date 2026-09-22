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

"""Vector Search 2.0 utility functions for collection and data object management."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from typing import TypeVar

from google.api_core import exceptions
from google.cloud import vectorsearch_v1beta

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _batched(
    items: Sequence[_T], size: int
) -> Iterator[tuple[int, Sequence[_T]]]:
    """Yield (start_index, batch) tuples for successive `size`-sized slices."""
    for start in range(0, len(items), size):
        yield start, items[start : start + size]


def ensure_collection(
    project_id: str,
    location: str,
    collection_id: str,
    embedding_model: str,
    embedding_dims: int,
) -> str:
    """Create the chunks collection if it doesn't exist. Returns collection_path.

    Schema is intentionally minimal — only what's needed for retrieval and
    cascade deletes. Full document text lives in the separate documents
    collection (see `ensure_documents_collection`) so it isn't duplicated
    on every chunk.
    """
    client = vectorsearch_v1beta.VectorSearchServiceClient()
    parent = f"projects/{project_id}/locations/{location}"
    collection_path = f"{parent}/collections/{collection_id}"

    try:
        client.get_collection(
            request=vectorsearch_v1beta.GetCollectionRequest(
                name=collection_path
            )
        )
        logger.info("Collection '%s' already exists.", collection_id)
        ensure_index(collection_path)
        return collection_path
    except exceptions.NotFound:
        pass

    logger.info("Creating collection '%s'...", collection_id)

    request = vectorsearch_v1beta.CreateCollectionRequest(
        parent=parent,
        collection_id=collection_id,
        collection={
            "data_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "chunk_id": {"type": "string"},
                    "chunk_text": {"type": "string"},
                    "gcs_uri": {"type": "string"},
                },
            },
            "vector_schema": {
                "text_embedding": {
                    "dense_vector": {
                        "dimensions": embedding_dims,
                        "vertex_embedding_config": {
                            "model_id": embedding_model,
                            "text_template": "{chunk_text}",
                            "task_type": "RETRIEVAL_DOCUMENT",
                        },
                    },
                },
            },
        },
    )

    operation = client.create_collection(request=request)
    logger.info("Waiting for collection creation to complete...")
    operation.result()
    logger.info("Collection created successfully.")
    ensure_index(collection_path)
    return collection_path


def ensure_index(
    collection_path: str,
    index_id: str = "default-index",
    index_field: str = "text_embedding",
    store_fields: list[str] | None = None,
    wait: bool = False,
) -> str:
    """Create an ANN index on the collection's embedding field if it doesn't exist.

    Without an index, semantic search is brute-force and stops working past
    a few thousand objects (returns FailedPrecondition). Creating it once
    on an empty collection is instant; the index then auto-builds in the
    background as data is added.

    IMPORTANT — store_fields:
        Once a collection has an ANN index, search responses no longer
        include the data fields by default — you only get back the
        data_object_id. To get the data inline (file_id, chunk_text, etc.)
        you must declare them as store_fields at INDEX CREATION TIME.
        store_fields cannot be changed later — you'd need to delete and
        recreate the index. So pass everything you'll need to read at
        query time.

    Args:
        collection_path: Full resource path of the collection.
        index_id: Identifier for the index (one per collection is enough).
        index_field: The embedding schema key to index.
        store_fields: Data fields to store inline in the index for fast
            retrieval at query time. If None, defaults to the chunks
            collection schema fields.
        wait: If True, block until the index is ready (can take 30-60 min
            for 100K+ existing objects). Async by default.

    Returns:
        Full resource path of the index.
    """
    if store_fields is None:
        store_fields = ["file_id", "chunk_id", "chunk_text", "gcs_uri"]

    client = vectorsearch_v1beta.VectorSearchServiceClient()
    index_path = f"{collection_path}/indexes/{index_id}"

    try:
        client.get_index(
            request=vectorsearch_v1beta.GetIndexRequest(name=index_path)
        )
        logger.info(
            "Index '%s' already exists on %s.", index_id, collection_path
        )
        return index_path
    except exceptions.NotFound:
        pass

    logger.info(
        "Creating index '%s' on %s (store_fields=%s)...",
        index_id,
        collection_path,
        store_fields,
    )
    operation = client.create_index(
        request=vectorsearch_v1beta.CreateIndexRequest(
            parent=collection_path,
            index_id=index_id,
            index=vectorsearch_v1beta.Index(
                index_field=index_field,
                store_fields=store_fields,
            ),
        )
    )

    if wait:
        logger.info("Waiting for index to build (may take several minutes)...")
        operation.result()
        logger.info("Index built successfully.")
    else:
        logger.info("Index creation started (async, building in background).")

    return index_path


def ensure_documents_collection(
    project_id: str,
    location: str,
    collection_id: str,
    embedding_model: str,
    embedding_dims: int,
) -> str:
    """Create the documents-by-file_id KV collection if it doesn't exist.

    Used as a sub-100ms point-lookup store for full document text at
    search time. data_object_id == file_id so retrieval is a direct
    GetDataObject call — no semantic query needed.

    The vector_schema is required by VS2 but is effectively unused: the
    text_template embeds only the file_id (a 32-char hex string), keeping
    embedding cost negligible. The vector itself is never queried.
    """
    client = vectorsearch_v1beta.VectorSearchServiceClient()
    parent = f"projects/{project_id}/locations/{location}"
    collection_path = f"{parent}/collections/{collection_id}"

    try:
        client.get_collection(
            request=vectorsearch_v1beta.GetCollectionRequest(
                name=collection_path
            )
        )
        logger.info("Documents collection '%s' already exists.", collection_id)
        return collection_path
    except exceptions.NotFound:
        pass

    logger.info("Creating documents collection '%s'...", collection_id)

    request = vectorsearch_v1beta.CreateCollectionRequest(
        parent=parent,
        collection_id=collection_id,
        collection={
            "data_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "gcs_uri": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            "vector_schema": {
                "text_embedding": {
                    "dense_vector": {
                        "dimensions": embedding_dims,
                        "vertex_embedding_config": {
                            "model_id": embedding_model,
                            "text_template": "{file_id}",
                            "task_type": "RETRIEVAL_DOCUMENT",
                        },
                    },
                },
            },
        },
    )

    operation = client.create_collection(request=request)
    logger.info("Waiting for documents collection creation to complete...")
    operation.result()
    logger.info("Documents collection created successfully.")
    return collection_path


def batch_create_data_objects(
    collection_path: str,
    chunks: list[dict],
    batch_size: int = 250,
    delay_seconds: float = 5.0,
) -> int:
    """Batch-create data objects in VS2. Returns count created.

    Each chunk dict must have: chunk_id, file_id, chunk_text, gcs_uri.
    Uses empty vectors — auto-generated by VS 2.0.
    """
    client = vectorsearch_v1beta.DataObjectServiceClient()
    created = 0

    for batch_start, batch in _batched(chunks, batch_size):
        batch_request = [
            {
                "data_object_id": chunk["chunk_id"],
                "data_object": {
                    "data": {
                        "file_id": chunk["file_id"],
                        "chunk_id": chunk["chunk_id"],
                        "chunk_text": chunk["chunk_text"],
                        "gcs_uri": chunk["gcs_uri"],
                    },
                    "vectors": {},
                },
            }
            for chunk in batch
        ]

        request = vectorsearch_v1beta.BatchCreateDataObjectsRequest(
            parent=collection_path,
            requests=batch_request,
        )

        for attempt in range(3):
            try:
                client.batch_create_data_objects(request)
                break
            except exceptions.ResourceExhausted:
                wait = delay_seconds * (2**attempt)
                logger.warning(
                    "Quota limit hit, waiting %.0fs before retry...", wait
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"Failed after 3 retries at batch starting at index {batch_start}"
            )

        created += len(batch)
        logger.info("Created %d/%d data objects...", created, len(chunks))

        if batch_start + batch_size < len(chunks):
            time.sleep(delay_seconds)

    return created


def query_data_objects_by_file_ids(
    collection_path: str,
    file_ids: list[str],
) -> list[str]:
    """Query VS2 for all data object names matching given file_ids.

    Returns list of full resource names. Uses pagination.
    """
    if not file_ids:
        return []

    search_client = vectorsearch_v1beta.DataObjectSearchServiceClient()
    all_names = []

    # Query in batches of 500 file_ids to avoid filter size limits
    for _, batch_ids in _batched(file_ids, 500):
        page_token = None

        while True:
            request = vectorsearch_v1beta.QueryDataObjectsRequest(
                parent=collection_path,
                filter={"file_id": {"$in": batch_ids}},
                **({"page_token": page_token} if page_token else {}),
            )
            response = search_client.query_data_objects(request)
            all_names.extend(obj.name for obj in response.data_objects)

            if not response.next_page_token:
                break
            page_token = response.next_page_token

    logger.info(
        "Found %d existing data objects for %d file_ids",
        len(all_names),
        len(file_ids),
    )
    return all_names


def batch_delete_data_objects(
    collection_path: str,
    resource_names: list[str],
) -> int:
    """Delete data objects by resource name. Returns count deleted."""
    if not resource_names:
        return 0

    client = vectorsearch_v1beta.DataObjectServiceClient()
    total_deleted = 0

    for _, batch_names in _batched(resource_names, 1000):
        delete_requests = [
            vectorsearch_v1beta.DeleteDataObjectRequest(name=name)
            for name in batch_names
        ]
        request = vectorsearch_v1beta.BatchDeleteDataObjectsRequest(
            parent=collection_path,
            requests=delete_requests,
        )
        client.batch_delete_data_objects(request)
        total_deleted += len(batch_names)

    logger.info("Deleted %d data objects from VS2", total_deleted)
    return total_deleted


def delete_data_objects_by_file_ids(
    collection_path: str,
    file_ids: list[str],
) -> int:
    """Delete all VS2 data objects belonging to given file_ids."""
    names = query_data_objects_by_file_ids(collection_path, file_ids)
    return batch_delete_data_objects(collection_path, names)


# ---------------------------------------------------------------------------
# Documents collection (KV by file_id)
# ---------------------------------------------------------------------------
# data_object_id == file_id, so reads/writes/deletes are direct point ops.
# No QueryDataObjects round-trip needed for cascade delete.


def upsert_documents(
    documents_collection_path: str,
    documents: list[dict],
    batch_size: int = 250,
    delay_seconds: float = 5.0,
) -> int:
    """Upsert documents into the documents collection by file_id.

    VS2 BatchCreate fails on existing IDs, so we emulate upsert by best-effort
    delete-then-create (DeleteDataObject is idempotent: returns NotFound, no error).
    Each doc dict must have: file_id, gcs_uri, content.
    """
    if not documents:
        return 0

    data_client = vectorsearch_v1beta.DataObjectServiceClient()
    upserted = 0

    for batch_start, batch in _batched(documents, batch_size):
        for doc in batch:
            name = f"{documents_collection_path}/dataObjects/{doc['file_id']}"
            try:
                data_client.delete_data_object(
                    request=vectorsearch_v1beta.DeleteDataObjectRequest(
                        name=name
                    )
                )
            except exceptions.NotFound:
                pass

        batch_request = [
            {
                "data_object_id": doc["file_id"],
                "data_object": {
                    "data": {
                        "file_id": doc["file_id"],
                        "gcs_uri": doc["gcs_uri"],
                        "content": doc["content"],
                    },
                    "vectors": {},
                },
            }
            for doc in batch
        ]

        request = vectorsearch_v1beta.BatchCreateDataObjectsRequest(
            parent=documents_collection_path,
            requests=batch_request,
        )

        for attempt in range(3):
            try:
                data_client.batch_create_data_objects(request)
                break
            except exceptions.ResourceExhausted:
                wait = delay_seconds * (2**attempt)
                logger.warning(
                    "Quota limit hit on documents upsert, waiting %.0fs...",
                    wait,
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"Documents upsert failed after 3 retries at batch starting at index {batch_start}"
            )

        upserted += len(batch)
        logger.info("Upserted %d/%d documents...", upserted, len(documents))

        if batch_start + batch_size < len(documents):
            time.sleep(delay_seconds)

    return upserted


def batch_get_documents(
    documents_collection_path: str,
    file_ids: list[str],
) -> dict[str, dict]:
    """Fetch documents by file_id. Returns {file_id: {gcs_uri, content}}.

    Missing file_ids are silently skipped — caller decides how to handle them.
    Uses parallel GetDataObject calls (VS2 has no native batch_get).
    """
    if not file_ids:
        return {}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = vectorsearch_v1beta.DataObjectServiceClient()

    def _get(file_id: str) -> tuple[str, dict | None]:
        name = f"{documents_collection_path}/dataObjects/{file_id}"
        try:
            obj = client.get_data_object(
                request=vectorsearch_v1beta.GetDataObjectRequest(name=name)
            )
            data = dict(obj.data) if obj.data else {}
            return file_id, {
                "gcs_uri": data.get("gcs_uri", ""),
                "content": data.get("content", ""),
            }
        except exceptions.NotFound:
            return file_id, None

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(file_ids), 32)) as pool:
        for fut in as_completed(pool.submit(_get, fid) for fid in file_ids):
            file_id, payload = fut.result()
            if payload is not None:
                results[file_id] = payload

    return results


def delete_documents_by_file_ids(
    documents_collection_path: str,
    file_ids: list[str],
) -> int:
    """Delete documents from the documents collection by file_id.

    Direct point delete since data_object_id == file_id — no search needed.
    NotFound is treated as success (idempotent).
    """
    if not file_ids:
        return 0

    client = vectorsearch_v1beta.DataObjectServiceClient()
    deleted = 0

    for _, batch in _batched(file_ids, 1000):
        delete_requests = [
            vectorsearch_v1beta.DeleteDataObjectRequest(
                name=f"{documents_collection_path}/dataObjects/{fid}"
            )
            for fid in batch
        ]
        request = vectorsearch_v1beta.BatchDeleteDataObjectsRequest(
            parent=documents_collection_path,
            requests=delete_requests,
        )
        client.batch_delete_data_objects(request)
        deleted += len(batch)

    logger.info("Deleted %d documents from VS2 documents collection", deleted)
    return deleted
