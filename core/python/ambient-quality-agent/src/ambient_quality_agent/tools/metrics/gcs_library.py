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

"""Loader for code-metric libraries stored in Cloud Storage.

Objects are loaded directly into memory as bytes rather than staged on local disk.
Because metric code executes in-process, write permissions on the GCS bucket
are equivalent to code deployment permissions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools import gcs
from ambient_quality_agent.tools.metrics.library import (
    MetricLibrary,
    load_library,
)

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

LIBRARY_PREFIX = "current/metrics/"
"""Cloud Storage object prefix containing the active metric library."""


def load_library_from_gcs(
    bucket: str,
    *,
    prefix: str = LIBRARY_PREFIX,
    client_factory: Callable[[], Any] | None = None,
) -> MetricLibrary:
    """Download metric definition files from Cloud Storage and parse them into a library.

    Sequence of operations:
    1. Obtains a Cloud Storage client via `client_factory` or `create_storage_client`.
    2. Lists all blobs under `prefix` in `bucket` and downloads their content as bytes.
    3. Strips the prefix from blob names to retain relative file paths.
    4. Parses the downloaded object mapping into a `MetricLibrary`.

    Args:
        bucket: Name of the GCS bucket containing metric files.
        prefix: Object name prefix under which the metrics are stored.
        client_factory: Optional factory callable returning a storage client.

    Returns:
        MetricLibrary parsed from the downloaded metric files.
    """
    client = (client_factory or gcs.create_storage_client)()
    objects = {
        blob.name[len(prefix) :].lstrip("/"): blob.download_as_bytes()
        for blob in client.list_blobs(bucket, prefix=prefix)
        if blob.name[len(prefix) :].lstrip("/")
    }
    logger.info(
        "Metric library: read %d object(s) from gs://%s/%s",
        len(objects),
        bucket,
        prefix,
    )
    return load_library(objects)
