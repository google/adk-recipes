#!/usr/bin/env python3
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

r"""Ingest products into Vertex AI Vector Search 2.0 Collection.

Reads products from BigQuery, creates a Vector Search 2.0 Collection
(if it doesn't exist), and inserts products one at a time. `AlreadyExists`
errors are caught so re-runs are idempotent. Embeddings are auto-generated
by the Collection's configured model.

Usage:
    python ingest_vertex_search.py \\
        --project-id my-project \\
        --collection-id my-products

    # Or use design-spec.md for defaults:
    python ingest_vertex_search.py --config design-spec.md
"""

# pylint: disable=line-too-long
# (error messages with collection_id hints and gcloud commands are intentionally long.)

import argparse
import logging
import pathlib
import sys
from typing import Any

from google.api_core import exceptions
from google.cloud import bigquery, vectorsearch
from google.protobuf import struct_pb2

# Allow imports from the script's own directory before pip install -e is run.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _setup_utils import (
    load_config,  # pylint: disable=wrong-import-position
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_FIELDS = ["name", "description", "category", "brand"]

# Vector dimensions each embedding model emits. Vector Search collections
# must be created with the correct dimension count or vectors are rejected
# / index results are wrong. gemini-embedding-001's native output is 3072,
# but we request the 768-dim projection for parity with text-embedding-*
# and lower storage/query cost. Add new entries as models are qualified.
EMBEDDING_MODEL_DIMENSIONS: dict[str, int] = {
    "gemini-embedding-001": 768,
    "text-embedding-004": 768,
    "text-embedding-005": 768,
    "text-multilingual-embedding-002": 768,
}
DEFAULT_EMBEDDING_DIMENSIONS = 768

# All product fields stored as data object fields
PRODUCT_DATA_FIELDS = {
    "product_id": "string",
    "name": "string",
    "price": "number",
    "description": "string",
    "category": "string",
    "brand": "string",
    "image_url": "string",
    "rating": "number",
    "stock": "integer",
}


def fetch_products(
    project_id: str, dataset_id: str, table_id: str
) -> list[dict[str, Any]]:
    """Fetch all products from a BigQuery table.

    Args:
        project_id: GCP project ID.
        dataset_id: BigQuery dataset name.
        table_id: BigQuery table name.

    Returns:
        List of product dicts (one per BQ row).
    """
    client = bigquery.Client(project=project_id)
    query = f"SELECT * FROM `{project_id}.{dataset_id}.{table_id}`"

    logger.info(
        "Fetching products from %s.%s.%s", project_id, dataset_id, table_id
    )
    results = client.query(query).result()
    products = [dict(row.items()) for row in results]
    logger.info("Fetched %d products", len(products))
    return products


def build_text_template(embedding_fields: list[str]) -> str:
    """Build the ``text_template`` for auto-embedding from field names.

    The template uses ``{field_name}`` placeholders that VS 2.0 resolves
    from the data object's data fields.

    Args:
        embedding_fields: Field names to include in the template.

    Returns:
        Pipe-separated template string, e.g.
        ``"name: {name} | description: {description}"``.
    """
    parts = [f"{field}: {{{field}}}" for field in embedding_fields]
    return " | ".join(parts)


def get_data_schema(products: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the data schema from known product fields.

    Only includes fields that exist in the actual data (sampled from the
    first 10 products).

    Args:
        products: Product dicts loaded from BigQuery.

    Returns:
        JSON-Schema-style dict ``{"type": "object", "properties": {...}}``
        with one property per detected field.
    """
    properties: dict[str, Any] = {}
    sample_keys: set[str] = set()
    for p in products[:10]:
        sample_keys.update(p.keys())

    for field, field_type in PRODUCT_DATA_FIELDS.items():
        if field in sample_keys:
            properties[field] = {"type": field_type}

    return {"type": "object", "properties": properties}


def create_collection_if_needed(  # pylint: disable=too-many-arguments
    project_id: str,
    location: str,
    collection_id: str,
    embedding_model: str,
    embedding_fields: list[str],
    data_schema: dict[str, Any],
) -> str:
    """Create a Vector Search 2.0 Collection if it doesn't exist.

    Args:
        project_id: GCP project ID.
        location: GCP region for the collection.
        collection_id: Collection ID (RFC1035-compliant, max 63 chars).
        embedding_model: Auto-embedding model ID (e.g.
            ``"gemini-embedding-001"``).
        embedding_fields: Fields used in the embedding text template.
        data_schema: JSON-Schema-style dict from :func:`get_data_schema`.

    Returns:
        Fully qualified collection resource path.
    """
    client = vectorsearch.VectorSearchServiceClient()
    parent = f"projects/{project_id}/locations/{location}"
    collection_name = f"{parent}/collections/{collection_id}"

    try:
        client.get_collection(
            request=vectorsearch.GetCollectionRequest(name=collection_name)
        )
        logger.info("Collection '%s' already exists", collection_id)
        return collection_name
    except exceptions.NotFound:
        pass

    text_template = build_text_template(embedding_fields)
    logger.info(
        "Creating collection '%s' with text_template: %s",
        collection_id,
        text_template,
    )

    dimensions = EMBEDDING_MODEL_DIMENSIONS.get(
        embedding_model, DEFAULT_EMBEDDING_DIMENSIONS
    )
    if embedding_model not in EMBEDDING_MODEL_DIMENSIONS:
        logger.warning(
            "Embedding model '%s' is not in EMBEDDING_MODEL_DIMENSIONS; "
            "using default %d dimensions. Add it to the lookup table in "
            "ingest_vertex_search.py if this is wrong for your model.",
            embedding_model,
            DEFAULT_EMBEDDING_DIMENSIONS,
        )

    request = vectorsearch.CreateCollectionRequest(
        parent=parent,
        collection_id=collection_id,
        collection={
            "data_schema": data_schema,
            "vector_schema": {
                "text_embedding": {
                    "dense_vector": {
                        "dimensions": dimensions,
                        "vertex_embedding_config": {
                            "model_id": embedding_model,
                            "text_template": text_template,
                            "task_type": "RETRIEVAL_DOCUMENT",
                        },
                    },
                },
            },
        },
    )

    try:
        operation = client.create_collection(request=request)
        logger.info("Waiting for collection creation to complete...")
        operation.result()
        logger.info("Collection created successfully")
    except Exception as e:  # pylint: disable=broad-exception-caught
        # The SDK often mangles the underlying API error into a confusing gRPC
        # StatusCode mismatch. Most common real causes:
        #   1. collection_id > 63 chars (RFC1035 limit)
        #   2. collection_id has uppercase or invalid characters
        #   3. location/region doesn't support Vector Search
        #   4. project lacks aiplatform.user IAM
        hints = []
        if len(collection_id) > 63:
            hints.append(
                f"collection_id is {len(collection_id)} chars (max 63 per RFC1035). "
                "Shorten it via the 'collection_id' key in design-spec.md."
            )
        if not collection_id.islower() or any(
            c.isupper() for c in collection_id
        ):
            hints.append(
                "collection_id must be all lowercase letters, digits, and dashes."
            )
        hint_text = (
            ("\n  Likely cause: " + " ".join(hints))
            if hints
            else (
                "\n  Possible causes: collection_id too long (>63), uppercase chars, "
                "region doesn't support Vector Search, or missing aiplatform.user IAM."
            )
        )
        logger.error(
            "Error creating collection '%s': %s%s\n"
            "  Manual cleanup if the collection was partially created:\n"
            "    gcloud ai vector-search-collections delete %s "
            "--region=%s --project=%s",
            collection_id,
            e,
            hint_text,
            collection_id,
            location,
            project_id,
        )
        sys.exit(1)

    return collection_name


def ingest_products(
    collection_path: str,
    products: list[dict[str, Any]],
) -> None:
    """Insert products into the Vector Search 2.0 Collection.

    Embeddings are auto-generated by the Collection's configured model.
    Uses individual inserts for reliability.
    """
    client = vectorsearch.DataObjectServiceClient()

    created = 0
    skipped = 0
    errors = 0

    for i, product in enumerate(products):
        product_id = str(product.get("product_id", ""))
        # Ensure ID is RFC1035 compliant (lowercase, starts with letter)
        safe_id = product_id.lower().replace("_", "-")
        if safe_id and not safe_id[0].isalpha():
            safe_id = "p-" + safe_id

        data_struct = struct_pb2.Struct()  # pylint: disable=no-member
        for field, field_type in PRODUCT_DATA_FIELDS.items():
            value = product.get(field)
            if value is None:
                continue
            # Preserve proper types for the schema
            if field_type == "number":
                data_struct.update({field: float(value)})
            elif field_type == "integer":
                data_struct.update({field: int(value)})
            else:
                data_struct.update({field: str(value)})

        try:
            client.create_data_object(
                parent=collection_path,
                data_object_id=safe_id,
                data_object=vectorsearch.DataObject(data=data_struct),
            )
            created += 1
        except exceptions.AlreadyExists:
            skipped += 1
        except Exception as e:  # pylint: disable=broad-exception-caught
            errors += 1
            logger.warning("Failed to insert %s: %s", safe_id, e)

        if (i + 1) % 50 == 0 or (i + 1) == len(products):
            logger.info("Processed %d/%d products", i + 1, len(products))

    logger.info(
        "Ingestion complete: %d created, %d skipped, %d errors",
        created,
        skipped,
        errors,
    )


def ingest_pipeline(  # pylint: disable=too-many-arguments
    *,
    project_id: str,
    location: str,
    dataset_id: str,
    table_id: str,
    collection_id: str,
    embedding_model: str,
    embedding_fields: list[str],
) -> str | None:
    """Run the Vector Search ingestion pipeline programmatically.

    Args:
        project_id: GCP project ID.
        location: GCP region for the collection.
        dataset_id: BigQuery dataset to read products from.
        table_id: BigQuery table to read products from.
        collection_id: Vector Search collection ID.
        embedding_model: Auto-embedding model ID.
        embedding_fields: Fields used in the embedding text template.

    Returns:
        The collection path on success (whether freshly created or
        already populated and skipped). ``None`` on error (e.g. no
        products found in BigQuery).
    """
    collection_path = f"projects/{project_id}/locations/{location}/collections/{collection_id}"

    # Re-run short-circuit: if the collection already exists AND is populated,
    # skip the BQ fetch + per-row insert loop.
    try:
        vs_client = vectorsearch.VectorSearchServiceClient()
        vs_client.get_collection(
            request=vectorsearch.GetCollectionRequest(name=collection_path)
        )
        search_client = vectorsearch.DataObjectSearchServiceClient()
        probe = search_client.search_data_objects(
            request=vectorsearch.SearchDataObjectsRequest(
                parent=collection_path,
                semantic_search=vectorsearch.SemanticSearch(
                    search_text="probe",
                    search_field="text_embedding",
                    task_type="QUESTION_ANSWERING",
                    top_k=1,
                    output_fields=vectorsearch.OutputFields(
                        data_fields=["product_id"]
                    ),
                ),
            )
        )
        if any(True for _ in probe):
            logger.info(
                "Collection '%s' already populated -- "
                "skipping ingestion (re-run idempotent path).",
                collection_id,
            )
            logger.info("Collection path: %s", collection_path)
            return collection_path
    except exceptions.NotFound:
        pass

    products = fetch_products(project_id, dataset_id, table_id)
    if not products:
        logger.error("No products found in BigQuery")
        return None

    data_schema = get_data_schema(products)
    collection_path = create_collection_if_needed(
        project_id,
        location,
        collection_id,
        embedding_model,
        embedding_fields,
        data_schema,
    )

    ingest_products(collection_path, products)

    logger.info(
        "Successfully ingested %d products into collection '%s'",
        len(products),
        collection_id,
    )
    logger.info("Collection path: %s", collection_path)
    return collection_path


def main():
    """Parse CLI arguments and run the Vector Search ingestion pipeline."""
    parser = argparse.ArgumentParser(
        description="Ingest products to Vertex AI Vector Search 2.0"
    )
    parser.add_argument(
        "--config",
        default="",
        help="Path to design-spec.md (provides defaults for other args)",
    )
    parser.add_argument("--project-id", help="GCP project ID")
    parser.add_argument("--location", default="us-central1", help="GCP region")
    parser.add_argument(
        "--dataset-id", default="retail_skill_products", help="BigQuery dataset"
    )
    parser.add_argument("--table-id", default="products", help="BigQuery table")
    parser.add_argument(
        "--collection-id",
        default="retail-skill-products-collection",
        help="Vector Search 2.0 Collection ID",
    )
    parser.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="Auto-embedding model (default: gemini-embedding-001)",
    )
    parser.add_argument(
        "--embedding-fields",
        default=",".join(DEFAULT_EMBEDDING_FIELDS),
        help="Comma-separated fields for embedding text template",
    )

    args = parser.parse_args()

    # Load design-spec.md defaults -- CLI args override config values
    if args.config:
        cfg = load_config(args.config)
        if not args.project_id:
            args.project_id = cfg.get("gcp_project_id", "")
        if args.location == "us-central1" and cfg.get("gcp_region"):
            args.location = cfg["gcp_region"]
        if cfg.get("dataset_id") and args.dataset_id == "retail_skill_products":
            args.dataset_id = cfg["dataset_id"]
        if cfg.get("table_id") and args.table_id == "products":
            args.table_id = cfg["table_id"]
        if (
            cfg.get("collection_id")
            and args.collection_id == "retail-skill-products-collection"
        ):
            args.collection_id = cfg["collection_id"]
        if cfg.get("embedding_model"):
            model_name = cfg["embedding_model"].split(" ")[0]
            if args.embedding_model == DEFAULT_EMBEDDING_MODEL:
                args.embedding_model = model_name
        if cfg.get("embedding_fields") and args.embedding_fields == ",".join(
            DEFAULT_EMBEDDING_FIELDS
        ):
            args.embedding_fields = cfg["embedding_fields"]

    if not args.project_id:
        parser.error(
            "--project-id is required (or set gcp_project_id in design-spec.md)"
        )

    embedding_fields = [f.strip() for f in args.embedding_fields.split(",")]

    result_path = ingest_pipeline(
        project_id=args.project_id,
        location=args.location,
        dataset_id=args.dataset_id,
        table_id=args.table_id,
        collection_id=args.collection_id,
        embedding_model=args.embedding_model,
        embedding_fields=embedding_fields,
    )
    if result_path:
        logger.info(
            "Set VECTOR_SEARCH_COLLECTION env var to this path in your agent."
        )


if __name__ == "__main__":
    main()
