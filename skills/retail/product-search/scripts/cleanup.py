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

r"""Clean up all GCP resources created by the product search agent.

Deletes BigQuery datasets, Vector Search 2.0 collections, and Cloud Run
services.

Usage:
    # Dry run (show what would be deleted, don't delete)
    python scripts/cleanup.py --config design-spec.md --dry-run

    # Delete everything
    python scripts/cleanup.py --config design-spec.md --confirm

    # Delete only specific resources
    python scripts/cleanup.py --config design-spec.md --confirm \\
        --only bigquery,vectorsearch
"""

# pylint: disable=line-too-long
# (error messages with resource paths and gcloud commands are intentionally long.)

import argparse
import logging
import pathlib
import subprocess
import sys
from typing import Any

# Allow imports from the script's own directory before pip install -e is run.
# MUST run before `from _setup_utils import ...` below, or the local import
# resolves via whatever fallback happens to be on sys.path (which is not
# guaranteed to be this script's dir when run as `python -m scripts.cleanup`).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _setup_utils import load_config
from google.api_core import exceptions
from google.cloud import bigquery, vectorsearch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALL_RESOURCE_TYPES = ["bigquery", "vectorsearch", "cloudrun"]
# Quick Start only creates BigQuery + Vector Search. Cloud Run is only used by
# the deploy extension, so we skip it by default to avoid slow `gcloud describe`
# roundtrips. Users who deployed to Cloud Run can opt in with --only cloudrun.
DEFAULT_RESOURCE_TYPES = ["bigquery", "vectorsearch"]

# Bound how long we'll wait for a single `gcloud describe` to answer. Without
# this, an unreachable network / wrong project can hang cleanup indefinitely.
GCLOUD_DESCRIBE_TIMEOUT_SEC = 15


def delete_bigquery(project_id: str, dataset_id: str, dry_run: bool) -> bool:
    """Delete a BigQuery dataset and all of its tables.

    Args:
        project_id: GCP project ID.
        dataset_id: Bare dataset name (no project prefix).
        dry_run: If True, log what would be deleted but don't delete.

    Returns:
        True on successful delete or when the dataset is already absent.
        False if the delete call raised an unexpected exception.
    """
    client = bigquery.Client(project=project_id)
    dataset_ref = f"{project_id}.{dataset_id}"

    try:
        client.get_dataset(dataset_ref)
    except exceptions.NotFound:
        logger.info("BigQuery dataset %s does not exist, skipping", dataset_ref)
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Permission denied / auth / network / etc. Return False so the
        # caller doesn't mistake this for "already deleted".
        logger.error("Failed to check BigQuery dataset %s: %s", dataset_ref, e)
        return False

    if dry_run:
        logger.info(
            "[DRY RUN] Would delete BigQuery dataset: %s (and all tables)",
            dataset_ref,
        )
        return True

    try:
        client.delete_dataset(
            dataset_ref, delete_contents=True, not_found_ok=True
        )
        logger.info("Deleted BigQuery dataset: %s", dataset_ref)
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Failed to delete BigQuery dataset %s: %s", dataset_ref, e)
        return False


def _drain_data_objects(collection_path: str) -> int:
    """Delete all data objects inside a Vector Search collection.

    The API rejects collection deletion while data objects exist, so we
    list them via semantic search (broad query) and batch-delete in chunks.

    Args:
        collection_path: Fully qualified Vector Search collection path
            (``projects/.../locations/.../collections/...``).

    Returns:
        The total number of data objects deleted.
    """
    search_client = vectorsearch.DataObjectSearchServiceClient()
    data_client = vectorsearch.DataObjectServiceClient()

    # Broad-stroke search to enumerate IDs server-side. top_k is capped by the
    # API; we loop until a search returns no new IDs (or we hit a sane ceiling).
    seen: set[str] = set()
    deleted_total = 0
    batch_size = 100
    max_iterations = (
        50  # 50 * 100 = 5000 objects; raise if you have a bigger collection
    )

    for _ in range(max_iterations):
        request = vectorsearch.SearchDataObjectsRequest(
            parent=collection_path,
            semantic_search=vectorsearch.SemanticSearch(
                search_text="product",  # broad query -- collection is product catalog
                search_field="text_embedding",
                task_type="QUESTION_ANSWERING",  # required by the API; matches scripts/retrievers.py
                top_k=batch_size,
                output_fields=vectorsearch.OutputFields(
                    data_fields=["product_id"]
                ),
            ),
        )

        batch_ids: list[str] = []
        for result in search_client.search_data_objects(request):
            object_name = result.data_object.name
            if object_name and object_name not in seen:
                seen.add(object_name)
                batch_ids.append(object_name)

        if not batch_ids:
            break

        delete_requests = [
            vectorsearch.DeleteDataObjectRequest(name=name)
            for name in batch_ids
        ]
        data_client.batch_delete_data_objects(
            request=vectorsearch.BatchDeleteDataObjectsRequest(
                parent=collection_path,
                requests=delete_requests,
            )
        )
        deleted_total += len(batch_ids)
        logger.info(
            "  Drained %d data objects (%d total)",
            len(batch_ids),
            deleted_total,
        )

    return deleted_total


def delete_vectorsearch_collection(
    project_id: str, location: str, collection_id: str, dry_run: bool
) -> bool:
    """Delete a Vector Search 2.0 Collection.

    The API requires the collection to be empty before deletion, so we
    first drain all contained data objects.

    Args:
        project_id: GCP project ID.
        location: GCP region of the collection.
        collection_id: Bare collection ID (no project/location prefix).
        dry_run: If True, log what would be deleted but don't delete.

    Returns:
        True on successful delete, when the collection is already absent,
        or when Vector Search isn't available in this region. False if the
        drain or delete call raised an unexpected exception.
    """
    client = vectorsearch.VectorSearchServiceClient()
    collection_name = f"projects/{project_id}/locations/{location}/collections/{collection_id}"

    try:
        client.get_collection(
            request=vectorsearch.GetCollectionRequest(name=collection_name)
        )
    except exceptions.NotFound:
        logger.info(
            "Vector Search collection %s does not exist, skipping",
            collection_id,
        )
        return True
    except exceptions.MethodNotImplemented:
        # Vector Search 2.0 isn't available in this region. Nothing to delete
        # because nothing could have been created here in the first place.
        logger.info(
            "Vector Search not available in %s; no collection to clean up.",
            location,
        )
        return True

    if dry_run:
        logger.info(
            "[DRY RUN] Would drain data objects and delete Vector Search collection: %s",
            collection_name,
        )
        return True

    try:
        logger.info(
            "Draining data objects from %s before deletion...", collection_id
        )
        drained = _drain_data_objects(collection_name)
        logger.info("Drained %d data object(s) from %s", drained, collection_id)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(
            "Failed to drain data objects from %s: %s\n"
            "  Collection deletion will fail until it is empty. Try the manual path:\n"
            "    gcloud ai vector-search-collections delete %s "
            "--region=%s --project=%s --force",
            collection_id,
            e,
            collection_id,
            location,
            project_id,
        )
        return False

    try:
        operation = client.delete_collection(
            request=vectorsearch.DeleteCollectionRequest(name=collection_name)
        )
        operation.result()
        logger.info("Deleted Vector Search collection: %s", collection_name)
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(
            "Failed to delete Vector Search collection %s: %s\n"
            "  Data objects were drained, but the delete still failed. Possible causes:\n"
            "  - An async embedding/index operation is still in flight (wait 2-3 min and retry)\n"
            "  - Stragglers remain that the drain pass missed; verify in Cloud Console.\n"
            "  Manual cleanup:\n"
            "    gcloud ai vector-search-collections delete %s "
            "--region=%s --project=%s --force",
            collection_id,
            e,
            collection_id,
            location,
            project_id,
        )
        return False


def delete_cloudrun(
    project_id: str, location: str, service_name: str, dry_run: bool
) -> bool:
    """Delete a Cloud Run service.

    Args:
        project_id: GCP project ID.
        location: GCP region of the service.
        service_name: Cloud Run service name.
        dry_run: If True, log what would be deleted but don't delete.

    Returns:
        True on successful delete, when the service is already absent, or
        when ``gcloud describe`` times out (treated as expected-absent).
        False if the delete call raised an unexpected error.
    """
    try:
        result = subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                service_name,
                "--region",
                location,
                "--project",
                project_id,
                "--format",
                "value(name)",
            ],
            capture_output=True,
            text=True,
            timeout=GCLOUD_DESCRIBE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "gcloud timed out checking Cloud Run service %s after %ds. Skipping. "
            "If you did deploy to Cloud Run, delete the service manually: "
            "gcloud run services delete %s --region=%s --project=%s",
            service_name,
            GCLOUD_DESCRIBE_TIMEOUT_SEC,
            service_name,
            location,
            project_id,
        )
        return True

    if result.returncode != 0:
        logger.info(
            "Cloud Run service %s does not exist, skipping", service_name
        )
        return True

    if dry_run:
        logger.info(
            "[DRY RUN] Would delete Cloud Run service: %s", service_name
        )
        return True

    try:
        subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "delete",
                service_name,
                "--region",
                location,
                "--project",
                project_id,
                "--quiet",
            ],
            check=True,
        )
        logger.info("Deleted Cloud Run service: %s", service_name)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(
            "Failed to delete Cloud Run service %s: %s", service_name, e
        )
        return False


def cleanup(
    config: dict[str, Any],
    dry_run: bool,
    only: list[str],
    dataset_id: str = "retail_skill_products",
) -> bool:
    """Run cleanup for all or selected resource types.

    Args:
        config: Parsed design-spec config dict.
        dry_run: If True, log what would be deleted but don't delete.
        only: Resource types to delete; subset of
            ``["bigquery", "vectorsearch", "cloudrun"]``.
        dataset_id: BigQuery dataset name to delete.

    Returns:
        True if every step (across the resource types in ``only``)
        succeeded; False if any step returned failure.
    """
    project_id = config.get("gcp_project_id", "")
    if not project_id:
        logger.error("gcp_project_id not set in config")
        sys.exit(1)

    location = config.get("gcp_region", "us-central1")
    project_name = config.get("project_name", "product-search")
    collection_id = config.get(
        "collection_id", "retail-skill-products-collection"
    )

    mode = "[DRY RUN] " if dry_run else ""
    logger.info("%sCleaning up resources for project: %s", mode, project_id)
    logger.info("%sResource types: %s", mode, ", ".join(only))
    logger.info("%sBigQuery dataset: %s", mode, dataset_id)
    logger.info("")

    failures: list[str] = []

    def _track(label: str, success: bool) -> None:
        if not success:
            failures.append(label)

    if "bigquery" in only:
        _track("bigquery", delete_bigquery(project_id, dataset_id, dry_run))

    if "vectorsearch" in only:
        _track(
            "vectorsearch (collection)",
            delete_vectorsearch_collection(
                project_id, location, collection_id, dry_run
            ),
        )

    if "cloudrun" in only:
        _track(
            "cloudrun",
            delete_cloudrun(project_id, location, project_name, dry_run),
        )

    logger.info("")
    if dry_run:
        logger.info("Dry run complete. No resources were deleted.")
        logger.info("Run with --confirm to actually delete.")
        return True

    if failures:
        logger.error(
            "Cleanup finished with errors. Failed steps: %s",
            ", ".join(failures),
        )
        logger.error("See messages above for the manual cleanup commands.")
        return False

    logger.info("Cleanup complete. All selected resources deleted.")
    return True


def main():
    """Parse CLI arguments and run cleanup."""
    parser = argparse.ArgumentParser(
        description="Clean up all GCP resources created by the product search agent"
    )
    parser.add_argument(
        "--config", required=True, help="Path to design-spec.md"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete resources (without this flag, runs in dry-run mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting",
    )
    parser.add_argument(
        "--only",
        default="",
        help=(
            f"Comma-separated resource types to delete. Valid: {','.join(ALL_RESOURCE_TYPES)}. "
            f"Default if omitted: {','.join(DEFAULT_RESOURCE_TYPES)} "
            "(Cloud Run excluded by default; opt in with --only cloudrun if you deployed to Cloud Run)."
        ),
    )
    parser.add_argument(
        "--dataset-id",
        default="retail_skill_products",
        help="BigQuery dataset name to delete (default: retail_skill_products)",
    )

    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        logger.error("Config file not found or empty: %s", args.config)
        sys.exit(1)

    # Honor dataset_id from design-spec.md unless overridden on the CLI.
    # Otherwise per-case suffixes get ignored and the wrong dataset is "deleted"
    # (i.e. cleanup looks for the default and reports a false success).
    dataset_id = args.dataset_id
    if dataset_id == "retail_skill_products" and config.get("dataset_id"):
        dataset_id = config["dataset_id"]

    dry_run = not args.confirm or args.dry_run

    if args.only:
        only = [r.strip() for r in args.only.split(",")]
        invalid = [r for r in only if r not in ALL_RESOURCE_TYPES]
        if invalid:
            parser.error(
                f"Invalid resource types: {invalid}. Valid: {ALL_RESOURCE_TYPES}"
            )
    else:
        only = DEFAULT_RESOURCE_TYPES

    if not dry_run:
        project_id = config.get("gcp_project_id", "unknown")
        print(
            f"\nYou are about to permanently delete GCP resources in project: {project_id}"
        )
        print(f"Resource types: {', '.join(only)}")
        try:
            answer = (
                input("\nAre you sure? Type 'yes' to confirm: ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            print("Aborted. No resources were deleted.")
            sys.exit(0)

    ok = cleanup(config, dry_run, only, dataset_id=dataset_id)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
