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

"""Google Cloud clients for the engine, each carrying AQuA's product token.

The engine builds every BigQuery, Cloud Storage, Gen AI, Gemini platform, Cloud
Tasks and Cloud Logging client here, so that each request it makes sends
``google-agents-cli-aqua/VERSION+agent_runtime`` (or ``+local``) for usage
analytics; see `ambient_quality_shared.usage_headers`. A test fails on a
client constructed anywhere else. ADK's model calls get the token in
`ambient_quality_agent.core.models.LocatedGemini`, and its session and artifact
services in `ambient_quality_agent.app_utils.services`.

Library imports are lazy, so importing this module costs nothing and tests can
replace a client class before a factory runs.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from ambient_quality_shared.usage_headers import (
    build_product_token,
    build_usage_headers,
    detect_platform,
)

if TYPE_CHECKING:
    import agentplatform
    from google import genai
    from google.api_core import gapic_v1
    from google.api_core.client_info import ClientInfo
    from google.cloud import bigquery, storage, tasks_v2
    from google.cloud.logging_v2.services.logging_service_v2 import (
        LoggingServiceV2Client,
    )
    from google.genai import types


def build_client_info() -> ClientInfo:
    """Builds the client info that puts the engine token on a REST client.

    Each call returns a new instance, because the client libraries add their
    own version fields to the one they are given.

    Returns:
        Client info whose user agent is the engine's product token, or None
        while usage analytics is off.
    """
    from google.api_core.client_info import ClientInfo

    return ClientInfo(
        user_agent=build_product_token(platform=detect_platform())
    )


def create_bigquery_client(project: str, location: str) -> bigquery.Client:
    """Creates a BigQuery client.

    Args:
        project: Project that runs and bills the jobs.
        location: Default location for jobs.

    Returns:
        The client.
    """
    from google.cloud import bigquery

    return bigquery.Client(
        project=project, location=location, client_info=build_client_info()
    )


def create_storage_client(project: str | None = None) -> storage.Client:
    """Creates a Cloud Storage client.

    Args:
        project: Project for requests that need one; None takes the
            credentials' default project.

    Returns:
        The client.
    """
    from google.cloud import storage

    return storage.Client(project=project, client_info=build_client_info())


def create_genai_client(
    *,
    project: str,
    location: str,
    retry_options: types.HttpRetryOptions | None = None,
    timeout_ms: int | None = None,
) -> genai.Client:
    """Creates a Gen AI client on Gemini platform.

    Args:
        project: Project that serves and bills the model calls.
        location: Location that serves the model.
        retry_options: How to retry failed calls; None uses the SDK's default.
        timeout_ms: HTTP timeout per attempt, in milliseconds; None uses the
            SDK's default, which is no timeout.

    Returns:
        The client.
    """
    from google import genai
    from google.genai import types

    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(
            headers=build_usage_headers(platform=detect_platform()),
            retry_options=retry_options,
            timeout=timeout_ms,
        ),
    )


def create_agentplatform_client(
    *, project: str, location: str
) -> agentplatform.Client:
    """Creates a Gemini platform client.

    Args:
        project: Project of the resources it calls.
        location: Location of the resources it calls.

    Returns:
        The client.
    """
    import agentplatform
    from google.genai import types

    return agentplatform.Client(
        project=project,
        location=location,
        http_options=types.HttpOptions(
            headers=build_usage_headers(platform=detect_platform())
        ),
    )


def create_cloud_tasks_client() -> tasks_v2.CloudTasksClient:
    """Creates a Cloud Tasks client.

    Returns:
        The client.
    """
    from google.cloud import tasks_v2
    from google.cloud.tasks_v2.services.cloud_tasks.transports.base import (
        DEFAULT_CLIENT_INFO,
    )

    return tasks_v2.CloudTasksClient(
        client_info=_build_grpc_client_info(DEFAULT_CLIENT_INFO)
    )


def create_logging_client() -> LoggingServiceV2Client:
    """Creates a Cloud Logging client.

    Returns:
        The client.
    """
    from google.cloud.logging_v2.services import logging_service_v2
    from google.cloud.logging_v2.services.logging_service_v2.transports.base import (
        DEFAULT_CLIENT_INFO,
    )

    return logging_service_v2.LoggingServiceV2Client(
        client_info=_build_grpc_client_info(DEFAULT_CLIENT_INFO)
    )


def _build_grpc_client_info(
    default: gapic_v1.client_info.ClientInfo,
) -> gapic_v1.client_info.ClientInfo:
    """Copies a gRPC library's default client info and adds the engine token.

    The copy keeps the library's own version fields, which a fresh client info
    would drop, and leaves the library's shared default untouched.

    Args:
        default: The library's `DEFAULT_CLIENT_INFO`.

    Returns:
        The copy.
    """
    client_info = copy.copy(default)
    client_info.user_agent = build_product_token(platform=detect_platform())
    return client_info
