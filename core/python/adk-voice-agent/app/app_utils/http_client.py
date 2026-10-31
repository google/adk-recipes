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

"""Pooled async HTTP client factory supporting transport injection in tests."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

_default_transport: httpx.AsyncBaseTransport | None = None
_pooled_clients: dict[tuple[int, str, int, float], _PooledAsyncClient] = {}


class _PooledAsyncClient(httpx.AsyncClient):
    async def __aenter__(self) -> _PooledAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: Any = None,
        exc_value: Any = None,
        traceback: Any = None,
    ) -> None:
        return None


def set_default_http_transport(
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    global _default_transport
    _default_transport = transport
    _pooled_clients.clear()


def default_http_transport() -> httpx.AsyncBaseTransport | None:
    return _default_transport


def _current_loop_id() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def get_pooled_http_client(
    base_url: str = "",
    *,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    effective_transport = (
        transport if transport is not None else _default_transport
    )
    key = (
        _current_loop_id(),
        base_url.rstrip("/"),
        id(effective_transport),
        float(timeout),
    )
    client = _pooled_clients.get(key)
    if client is None or client.is_closed:
        client = _PooledAsyncClient(
            timeout=timeout,
            transport=effective_transport,
        )
        _pooled_clients[key] = client
    return client


def get_http_client(
    *,
    timeout: float = 8.0,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    if headers is None:
        return get_pooled_http_client(
            timeout=timeout,
            transport=transport,
        )
    return httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        transport=transport if transport is not None else _default_transport,
    )
