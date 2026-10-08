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

"""Cloud Storage helpers shared across AQA.

Provides atomic marker creation for idempotent trigger processing, ensuring that
redelivered events do not start duplicate investigations. Also provides a centralized
factory for `storage.Client` instances.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from google.cloud import storage


def create_storage_client() -> storage.Client:
    """Create a Cloud Storage client.

    Uses a lazy import so tests can inject a fake client without requiring
    network access or credentials.

    Returns:
        storage.Client instance.
    """
    from google.cloud import storage

    return storage.Client()


def build_per_thread_client_factory() -> Callable[[], storage.Client]:
    """Returns a factory that builds one Cloud Storage client per thread and reuses it.

    For a store read object by object, such as a source snapshot searched file
    by file, where a client per call would repeat the credential lookup for
    every object. One per thread, because a client is not documented as safe to
    share across the worker threads `asyncio.to_thread` runs reads on.

    Returns:
        A callable returning this thread's client.
    """
    local = threading.local()

    def get_client() -> storage.Client:
        client = getattr(local, "client", None)
        if client is None:
            client = local.client = create_storage_client()
        return client

    return get_client


def claim_once(bucket: str, object_name: str) -> bool:
    """Atomically create a marker object if it does not already exist.

    Deduplicates operations using Cloud Storage preconditions:
    1. Uploads an empty object with precondition `if_generation_match=0` (create-if-absent).
    2. Returns True if this call created the object.
    3. Handles `PreconditionFailed` and returns False if the object already exists.
    4. Propagates any other error (auth, network, quota) to avoid masking failures.

    Args:
        bucket: Target GCS bucket name.
        object_name: Object path representing the marker.

    Returns:
        True if this call created the marker object; False if it already existed.
    """
    from google.api_core import exceptions as api_exceptions

    blob = create_storage_client().bucket(bucket).blob(object_name)
    try:
        blob.upload_from_string(b"", if_generation_match=0)
    except api_exceptions.PreconditionFailed:
        return False
    return True
