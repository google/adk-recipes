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

"""`ObjectStore` over a Cloud Storage bucket, the deployed implementation.

Two buckets AQuA owns are kept this way, and the engine service account can
read and write both. The jobs bucket's one lifecycle rule is scoped to
`aqa-idempotency-keys/`, so nothing kept there through this store expires; the
UI service account can read it too. The source bucket's objects expire after
`source_snapshot_retention_days`.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools import gcs
from ambient_quality_agent.tools.objects.store import StorageNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Callable


class GcsObjectStore:
    """Objects at `gs://<bucket>/<name>`."""

    def __init__(
        self, bucket: str, *, client_factory: Callable[[], Any] | None = None
    ) -> None:
        """Initializes the store; nothing is read until the first call.

        Args:
            bucket: Bucket name; empty when the deployment has none, which every
                operation except `uri` then refuses.
            client_factory: Returns a `storage.Client`; defaults to
                `gcs.create_storage_client`.
        """
        self._bucket_name = bucket
        self._client_factory = client_factory or gcs.create_storage_client

    def _client(self) -> Any:
        if not self._bucket_name:
            raise StorageNotConfiguredError(
                "No jobs bucket is configured, so there is nowhere to keep the "
                "goal, the memories or the agent configuration. Set "
                "AQA_JOBS_GCS_BUCKET."
            )
        return self._client_factory()

    def read_text(self, name: str) -> str | None:
        from google.api_core import exceptions as api_exceptions

        blob = self._client().bucket(self._bucket_name).blob(name)
        try:
            return blob.download_as_text()
        except api_exceptions.NotFound:
            return None

    def write_text(
        self, name: str, text: str, *, overwrite: bool = True
    ) -> bool:
        from google.api_core import exceptions as api_exceptions

        options: dict[str, Any] = (
            {} if overwrite else {"if_generation_match": 0}
        )
        content_type, _ = mimetypes.guess_type(name)
        if content_type:
            options["content_type"] = content_type
        blob = self._client().bucket(self._bucket_name).blob(name)
        try:
            blob.upload_from_string(text.encode("utf-8"), **options)
        except api_exceptions.PreconditionFailed:
            return False
        return True

    def read_bytes(self, name: str) -> bytes | None:
        from google.api_core import exceptions as api_exceptions

        blob = self._client().bucket(self._bucket_name).blob(name)
        try:
            return blob.download_as_bytes()
        except api_exceptions.NotFound:
            return None

    def write_bytes(self, name: str, data: bytes) -> None:
        self._client().bucket(self._bucket_name).blob(name).upload_from_string(
            data
        )

    def delete(self, name: str) -> bool:
        from google.api_core import exceptions as api_exceptions

        try:
            self._client().bucket(self._bucket_name).blob(name).delete()
        except api_exceptions.NotFound:
            return False
        return True

    def list_names(self, prefix: str) -> list[str]:
        blobs = self._client().list_blobs(self._bucket_name, prefix=prefix)
        return sorted(blob.name for blob in blobs)

    def list_prefixes(self, prefix: str) -> list[str]:
        iterator = self._client().list_blobs(
            self._bucket_name, prefix=prefix, delimiter="/"
        )
        # The iterator fills `prefixes` only as its pages are consumed.
        for _ in iterator:
            pass
        return sorted(iterator.prefixes)

    def uri(self, name: str) -> str:
        return f"gs://{self._bucket_name}/{name}"
