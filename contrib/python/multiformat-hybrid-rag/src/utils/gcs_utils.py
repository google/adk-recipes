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

"""GCS utility functions for bucket and blob operations."""

from __future__ import annotations

import logging

from google.cloud import storage

logger = logging.getLogger(__name__)


def get_client(project_id: str) -> storage.Client:
    return storage.Client(project=project_id)


def ensure_bucket(
    project_id: str, bucket_name: str, location: str
) -> storage.Bucket:
    """Create the bucket if it doesn't exist. Returns the Bucket object."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    if not bucket.exists():
        bucket.create(location=location)
        logger.info("Created bucket: %s", bucket_name)
    else:
        logger.info("Bucket already exists: %s", bucket_name)
    return bucket


def upload_file(
    project_id: str,
    bucket_name: str,
    source_path: str,
    destination_blob: str,
) -> str:
    """Upload a local file to GCS. Returns gs:// URI."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob)
    blob.upload_from_filename(source_path)
    uri = f"gs://{bucket_name}/{destination_blob}"
    logger.info("Uploaded %s -> %s", source_path, uri)
    return uri


def upload_text(
    project_id: str,
    bucket_name: str,
    destination_blob: str,
    content: str,
) -> str:
    """Upload text content to GCS. Returns gs:// URI."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob)
    blob.upload_from_string(content, content_type="text/plain")
    uri = f"gs://{bucket_name}/{destination_blob}"
    logger.info("Uploaded text -> %s", uri)
    return uri


def delete_blob(project_id: str, bucket_name: str, blob_name: str) -> None:
    """Delete a single blob from GCS."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.delete()
    logger.info("Deleted gs://%s/%s", bucket_name, blob_name)


def delete_all_blobs(project_id: str, bucket_name: str, prefix: str) -> int:
    """Delete all blobs under a prefix. Returns count deleted."""
    client = get_client(project_id)
    blobs = list(client.list_blobs(bucket_name, prefix=prefix))
    for blob in blobs:
        blob.delete()
    logger.info(
        "Deleted %d blobs under gs://%s/%s", len(blobs), bucket_name, prefix
    )
    return len(blobs)


def list_blobs(
    project_id: str, bucket_name: str, prefix: str
) -> list[storage.Blob]:
    """List all blobs under a prefix."""
    client = get_client(project_id)
    return list(client.list_blobs(bucket_name, prefix=prefix))


def read_blob_bytes(project_id: str, bucket_name: str, blob_name: str) -> bytes:
    """Read a blob's content as raw bytes."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    return blob.download_as_bytes()


def get_blob_content_type(
    project_id: str, bucket_name: str, blob_name: str
) -> str:
    """Get the content_type metadata from a GCS blob."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.reload()
    return blob.content_type or "application/octet-stream"


def read_blob_text(project_id: str, bucket_name: str, blob_name: str) -> str:
    """Read a blob's content as UTF-8 text."""
    client = get_client(project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    return blob.download_as_text()
