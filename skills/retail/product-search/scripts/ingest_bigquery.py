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

r"""Ingest product catalog data into BigQuery.

Supports CSV and JSON source formats from GCS or local files.
Validates products against the configured schema before loading.

Usage:
    python ingest_bigquery.py \\
        --project-id my-project \\
        --gcs-bucket my-project-products \\
        --gcs-path products.csv

    python ingest_bigquery.py \\
        --project-id my-project \\
        --local-file data/products.json \\
        --format json

    # Or use design-spec.md for defaults:
    python ingest_bigquery.py --config design-spec.md \\
        --local-file data/products.csv
"""

# pylint: disable=line-too-long
# (validation error messages and argparse help strings are intentionally long.)

import argparse
import csv
import io
import json
import logging
import pathlib
import sys
from typing import Any

from google.cloud import bigquery, storage

# Allow imports from the script's own directory before pip install -e is run.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _setup_utils import (
    load_config,  # pylint: disable=wrong-import-position
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure these for your product schema.
# Basic: product_id, name, price, description
# Standard: + category, brand, image_url
# Extended: + rating, stock, manufacturer
# Full: + variants, tags, specifications, reviews
REQUIRED_FIELDS = ["product_id", "name", "price", "description"]

SCHEMA = [
    bigquery.SchemaField("product_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("price", "FLOAT64", mode="REQUIRED"),
    bigquery.SchemaField("description", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("category", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("brand", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("image_url", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("rating", "FLOAT64", mode="NULLABLE"),
    bigquery.SchemaField("stock", "INT64", mode="NULLABLE"),
]


def validate_product(product: dict[str, Any], row_num: int) -> list[str]:
    """Validate a single product record.

    Args:
        product: One product as a dict from the source file.
        row_num: 1-based row number for error messages.

    Returns:
        List of error strings for this row (empty if valid).
    """
    errors = []
    for field in REQUIRED_FIELDS:
        if field not in product or not product[field]:
            errors.append(f"Row {row_num}: missing required field '{field}'")

    if product.get("price"):
        try:
            float(product["price"])
        except (ValueError, TypeError):
            errors.append(
                f"Row {row_num}: 'price' must be numeric, got '{product['price']}'"
            )

    if product.get("stock"):
        try:
            int(product["stock"])
        except (ValueError, TypeError):
            errors.append(
                f"Row {row_num}: 'stock' must be an integer, got '{product['stock']}'"
            )

    return errors


def convert_types(product: dict[str, Any]) -> dict[str, Any]:
    """Convert string values to proper types for BigQuery.

    Args:
        product: Raw product dict (typically with string-typed values
            from CSV parsing).

    Returns:
        New dict with ``price``/``rating`` cast to float, ``stock`` cast
        to int, and string fields passed through.
    """
    converted = {}
    converted["product_id"] = product.get("product_id", "")
    converted["name"] = product.get("name", "")
    converted["description"] = product.get("description", "")

    if product.get("price"):
        converted["price"] = float(product["price"])

    if product.get("rating"):
        converted["rating"] = float(product["rating"])

    # `stock: 0` is a legit value (out-of-stock product); only skip when
    # the field is missing or an empty string from CSV.
    stock = product.get("stock")
    if stock is not None and stock != "":
        converted["stock"] = int(stock)

    for field in ["category", "brand", "image_url"]:
        if field in product:
            converted[field] = product[field]

    return converted


def load_from_csv(source: str) -> list[dict[str, Any]]:
    """Load products from CSV (GCS URI or local path).

    Args:
        source: Either a ``gs://bucket/path.csv`` URI or a local filesystem
            path to a CSV file.

    Returns:
        List of validated, type-converted product dicts.
    """
    if source.startswith("gs://"):
        parts = source.replace("gs://", "").split("/", 1)
        client = storage.Client()
        blob = client.bucket(parts[0]).blob(parts[1])
        content = blob.download_as_text()
        reader = csv.DictReader(content.splitlines())
    else:
        with open(source, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return _validate_and_convert(list(reader))

    return _validate_and_convert(list(reader))


def load_from_json(source: str) -> list[dict[str, Any]]:
    """Load products from JSON or JSONL (GCS URI or local path).

    Args:
        source: Either a ``gs://bucket/path.json[l]`` URI or a local
            filesystem path. JSONL is detected from the suffix.

    Returns:
        List of validated, type-converted product dicts.
    """
    if source.startswith("gs://"):
        parts = source.replace("gs://", "").split("/", 1)
        client = storage.Client()
        blob = client.bucket(parts[0]).blob(parts[1])
        content = blob.download_as_text()
    else:
        content = pathlib.Path(source).read_text(encoding="utf-8")

    if source.endswith(".jsonl"):
        raw = [
            json.loads(line)
            for line in content.strip().splitlines()
            if line.strip()
        ]
    else:
        parsed = json.loads(content)
        raw = parsed if isinstance(parsed, list) else parsed.get("products", [])

    return _validate_and_convert(raw)


def _validate_and_convert(
    raw_products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and type-convert a list of product dicts.

    Logs a summary of skipped rows when validation produces errors.

    Args:
        raw_products: Raw dicts straight from the source loader.

    Returns:
        List of cleaned, type-converted product dicts (skipped rows are
        excluded).
    """
    products = []
    all_errors = []

    for i, product in enumerate(raw_products, start=1):
        errors = validate_product(product, i)
        if errors:
            all_errors.extend(errors)
            continue
        products.append(convert_types(product))

    if all_errors:
        logger.warning(
            "Skipped rows with %d validation errors:", len(all_errors)
        )
        for err in all_errors[:10]:
            logger.warning("  %s", err)
        if len(all_errors) > 10:
            logger.warning("  ... and %d more", len(all_errors) - 10)

    logger.info("Loaded %d valid products", len(products))
    return products


def _ensure_dataset_and_table(
    client: bigquery.Client,
    dataset_ref: str,
    table_ref: str,
    table_id: str,
    if_exists: str,
) -> bool:
    """Ensure the BigQuery dataset and table exist based on if_exists policy.

    Args:
        client: An initialized BigQuery client.
        dataset_ref: Fully qualified dataset reference (``project.dataset``).
        table_ref: Fully qualified table reference (``project.dataset.table``).
        table_id: Bare table name (used when creating the table).
        if_exists: Behavior when the dataset already exists: ``"error"``,
            ``"skip"``, or ``"rename"`` (interactive only).

    Returns:
        ``True`` if ingestion should continue (dataset/table ready),
        ``False`` if the existing data should be left alone (skip path).

    Raises:
        ValueError: When ``if_exists="rename"`` and the user supplied a new
            dataset name; the new name is the exception's argument. The
            caller must re-derive refs from this name and re-invoke.
    """
    try:
        client.get_dataset(dataset_ref)
        dataset_exists = True
    except Exception:  # pylint: disable=broad-exception-caught
        dataset_exists = False

    if dataset_exists:
        if if_exists == "skip":
            try:
                client.get_table(table_ref)
                count_query = f"SELECT COUNT(*) AS n FROM `{table_ref}`"
                row_count = next(iter(client.query(count_query).result())).n
                if row_count > 0:
                    logger.info(
                        "Dataset %s and table %s already exist with %d rows "
                        "-- skipping ingestion.",
                        dataset_ref,
                        table_ref,
                        row_count,
                    )
                    return False
                logger.info(
                    "Table %s exists but is empty -- loading rows.", table_ref
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.info(
                    "Dataset %s exists but table %s missing -- creating table.",
                    dataset_ref,
                    table_ref,
                )
                table = bigquery.Table(table_ref, schema=SCHEMA)
                client.create_table(table)
        elif if_exists == "rename" and sys.stdin.isatty():
            logger.warning("Dataset %s already exists.", dataset_ref)
            try:
                new_name = input(
                    "  Enter a different dataset name (or Ctrl+C to cancel): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                logger.info("Cancelled.")
                sys.exit(0)
            if not new_name:
                logger.error("No name provided. Exiting.")
                sys.exit(1)
            # Caller must re-derive refs from new_name; signal by returning None
            # We return the new dataset ID via sys.exit to keep signature simple.
            # Raise so the caller can re-try with new name.
            raise ValueError(new_name)
        else:
            logger.error(
                "Dataset %s already exists. "
                "Re-run with --if-exists skip to reuse it, or --dataset-id <new_name>.",
                dataset_ref,
            )
            sys.exit(1)

    if not dataset_exists:
        logger.info("Creating dataset %s", dataset_ref)
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = "US"
        client.create_dataset(dataset)

        logger.info("Creating table %s", table_ref)
        table = bigquery.Table(table_ref, schema=SCHEMA)
        client.create_table(table)

    return True


def ingest(
    project_id: str,
    dataset_id: str,
    table_id: str,
    source: str,
    source_format: str = "csv",
    if_exists: str = "error",
):
    """Ingest products into BigQuery.

    if_exists controls behavior when the target dataset already exists:
      - "error" (default): fail with a clear message
      - "skip": no-op if the table already has rows (idempotent re-runs)
      - "rename": prompt for a new name (interactive only)
    """
    client = bigquery.Client(project=project_id)

    dataset_ref = f"{project_id}.{dataset_id}"
    table_ref = f"{project_id}.{dataset_id}.{table_id}"

    try:
        should_continue = _ensure_dataset_and_table(
            client, dataset_ref, table_ref, table_id, if_exists
        )
    except ValueError as new_name:
        # "rename" path: recurse with the new dataset name.
        dataset_id = str(new_name)
        dataset_ref = f"{project_id}.{dataset_id}"
        table_ref = f"{project_id}.{dataset_id}.{table_id}"
        try:
            client.get_dataset(dataset_ref)
            logger.error(
                "Dataset %s also exists. Re-run with --dataset-id <unique_name>.",
                dataset_ref,
            )
            sys.exit(1)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        should_continue = _ensure_dataset_and_table(
            client, dataset_ref, table_ref, table_id, "error"
        )

    if not should_continue:
        return

    # Load products
    if source_format == "json":
        products = load_from_json(source)
    else:
        products = load_from_csv(source)

    if not products:
        logger.error("No valid products to ingest")
        sys.exit(1)

    # Use a load job (batch) instead of streaming insert. Streaming inserts go
    # through a buffer that can keep stale dataset-uuid references for ~5-10 min
    # after a delete, which causes "Dataset is deleted" errors when the same
    # dataset name is recreated quickly (e.g. test re-runs). Load jobs route
    # through a different path with no such race.
    job_config = bigquery.LoadJobConfig(
        schema=SCHEMA,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    ndjson = "\n".join(json.dumps(p) for p in products).encode("utf-8")
    load_job = client.load_table_from_file(
        io.BytesIO(ndjson),
        table_ref,
        job_config=job_config,
    )
    load_job.result()  # Wait for completion; raises on error.
    if load_job.errors:
        logger.error("Load job errors: %s", load_job.errors)
        sys.exit(1)

    logger.info(
        "Successfully ingested %d products to %s", len(products), table_ref
    )


def main():
    """Parse CLI arguments and ingest products to BigQuery."""
    parser = argparse.ArgumentParser(
        description="Ingest product catalog to BigQuery"
    )
    parser.add_argument(
        "--config",
        default="",
        help="Path to design-spec.md (provides defaults for other args)",
    )
    parser.add_argument("--project-id", help="GCP project ID")
    parser.add_argument(
        "--dataset-id",
        default="retail_skill_products",
        help="BigQuery dataset ID",
    )
    parser.add_argument(
        "--table-id", default="products", help="BigQuery table ID"
    )
    parser.add_argument(
        "--gcs-bucket", help="GCS bucket name (used with --gcs-path)"
    )
    parser.add_argument("--gcs-path", help="Path to data file in GCS bucket")
    parser.add_argument("--local-file", help="Path to local data file")
    parser.add_argument(
        "--format", choices=["csv", "json"], default="csv", help="Source format"
    )
    parser.add_argument(
        "--if-exists",
        choices=["error", "skip", "rename"],
        default="error",
        help=(
            "Behavior when dataset already exists: error (default), "
            "skip (idempotent re-run), rename (interactive)"
        ),
    )

    args = parser.parse_args()

    # Load design-spec.md defaults -- CLI args override config values
    if args.config:
        cfg = load_config(args.config)
        if not args.project_id:
            args.project_id = cfg.get("gcp_project_id", "")

    if not args.project_id:
        parser.error(
            "--project-id is required (or set gcp_project_id in design-spec.md)"
        )
    if args.local_file:
        source = args.local_file
    elif args.gcs_bucket and args.gcs_path:
        source = f"gs://{args.gcs_bucket}/{args.gcs_path}"
    else:
        parser.error(
            "Provide either --local-file or both --gcs-bucket and --gcs-path"
        )

    ingest(
        project_id=args.project_id,
        dataset_id=args.dataset_id,
        table_id=args.table_id,
        source=source,
        source_format=args.format,
        if_exists=args.if_exists,
    )


if __name__ == "__main__":
    main()
