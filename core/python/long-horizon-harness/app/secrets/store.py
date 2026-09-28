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

"""Per-user secret storage backed by one Secret Manager secret per user.

The secret holds a JSON blob ``{name: {"value": str, "created_at": int_ms}}``.
A short TTL cache keeps Secret Manager access well under the 90,000/min quota
and lets multiple Cloud Run instances converge after an edit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

MAX_BLOB_BYTES = 64 * 1024  # Secret Manager payload limit


class SecretTooLargeError(ValueError):
    """Raised when a set would push the user's blob over MAX_BLOB_BYTES."""


def _now_ms_default() -> int:
    return int(time.time() * 1000)


@runtime_checkable
class SecretStore(Protocol):
    async def get_all(self, user_id: str) -> dict[str, str]: ...
    async def list_names(self, user_id: str) -> list[dict[str, Any]]: ...
    async def set_secret(self, user_id: str, name: str, value: str) -> None: ...
    async def set_many(self, user_id: str, mapping: dict[str, str]) -> None: ...
    async def delete_secret(self, user_id: str, name: str) -> bool: ...
    def invalidate(self, user_id: str) -> None: ...


class SecretManagerStore:
    def __init__(
        self,
        *,
        client: Any,
        project_id: str,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = _now_ms_default,
    ) -> None:
        self._client = client
        self._project_id = project_id
        self._ttl_s = ttl_s
        self._clock = clock
        self._now_ms = now_ms
        self._cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}

    def _secret_id(self, user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        return f"lha-secret-{digest}"

    def _secret_name(self, user_id: str) -> str:
        return f"projects/{self._project_id}/secrets/{self._secret_id(user_id)}"

    def _version_name(self, user_id: str, version: str) -> str:
        return f"{self._secret_name(user_id)}/versions/{version}"

    async def _fetch_blob(self, user_id: str) -> dict[str, dict[str, Any]]:
        from google.api_core.exceptions import NotFound

        try:
            resp = await self._client.access_secret_version(
                request={"name": self._version_name(user_id, "latest")}
            )
        except NotFound:
            # No secret yet for this user; every other error (permission,
            # disabled version, quota, transport) is real and must propagate.
            return {}
        try:
            blob = json.loads(resp.payload.data)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "secret blob for user is not valid JSON; treating as empty"
            )
            return {}
        return blob if isinstance(blob, dict) else {}

    async def _cached_blob(self, user_id: str) -> dict[str, dict[str, Any]]:
        now = self._clock()
        hit = self._cache.get(user_id)
        if hit is not None and hit[0] > now:
            return hit[1]
        blob = await self._fetch_blob(user_id)
        self._cache[user_id] = (now + self._ttl_s, blob)
        return blob

    def invalidate(self, user_id: str) -> None:
        self._cache.pop(user_id, None)

    async def get_all(self, user_id: str) -> dict[str, str]:
        blob = await self._cached_blob(user_id)
        return {
            name: entry["value"]
            for name, entry in blob.items()
            if isinstance(entry, dict) and "value" in entry
        }

    async def list_names(self, user_id: str) -> list[dict[str, Any]]:
        blob = await self._cached_blob(user_id)
        return [
            {"name": name, "created_at": entry.get("created_at")}
            for name, entry in sorted(blob.items())
        ]

    async def _ensure_secret(self, user_id: str) -> None:
        from google.api_core.exceptions import AlreadyExists

        try:
            await self._client.create_secret(
                request={
                    "parent": f"projects/{self._project_id}",
                    "secret_id": self._secret_id(user_id),
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        except AlreadyExists:
            pass

    async def _write_blob(
        self, user_id: str, blob: dict[str, dict[str, Any]]
    ) -> None:
        data = json.dumps(blob).encode("utf-8")
        if len(data) > MAX_BLOB_BYTES:
            raise SecretTooLargeError(
                f"secrets blob is {len(data)} bytes, over the "
                f"{MAX_BLOB_BYTES}-byte limit"
            )
        await self._ensure_secret(user_id)
        await self._client.add_secret_version(
            request={
                "parent": self._secret_name(user_id),
                "payload": {"data": data},
            }
        )
        self.invalidate(user_id)

    async def set_secret(self, user_id: str, name: str, value: str) -> None:
        blob = dict(await self._fetch_blob(user_id))
        blob[name] = {"value": value, "created_at": self._now_ms()}
        await self._write_blob(user_id, blob)

    async def set_many(self, user_id: str, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        blob = dict(await self._fetch_blob(user_id))
        now = self._now_ms()
        for name, value in mapping.items():
            blob[name] = {"value": value, "created_at": now}
        await self._write_blob(user_id, blob)

    async def delete_secret(self, user_id: str, name: str) -> bool:
        blob = dict(await self._fetch_blob(user_id))
        if name not in blob:
            return False
        del blob[name]
        await self._write_blob(user_id, blob)
        return True


class InMemorySecretStore:
    """Zero-dependency secret store for tests and non-GCP deployments."""

    def __init__(self, *, now_ms: Callable[[], int] = _now_ms_default) -> None:
        self._now_ms = now_ms
        self._blobs: dict[str, dict[str, dict[str, Any]]] = {}

    def _blob(self, user_id: str) -> dict[str, dict[str, Any]]:
        return self._blobs.setdefault(user_id, {})

    def invalidate(self, user_id: str) -> None:
        return None

    async def get_all(self, user_id: str) -> dict[str, str]:
        return {
            name: entry["value"]
            for name, entry in self._blob(user_id).items()
            if isinstance(entry, dict) and "value" in entry
        }

    async def list_names(self, user_id: str) -> list[dict[str, Any]]:
        return [
            {"name": name, "created_at": entry.get("created_at")}
            for name, entry in sorted(self._blob(user_id).items())
        ]

    def _guard_size(self, blob: dict[str, dict[str, Any]]) -> None:
        if len(json.dumps(blob).encode("utf-8")) > MAX_BLOB_BYTES:
            raise SecretTooLargeError(
                f"secrets blob is over {MAX_BLOB_BYTES} bytes"
            )

    async def set_secret(self, user_id: str, name: str, value: str) -> None:
        blob = dict(self._blob(user_id))
        blob[name] = {"value": value, "created_at": self._now_ms()}
        self._guard_size(blob)
        self._blobs[user_id] = blob

    async def set_many(self, user_id: str, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        blob = dict(self._blob(user_id))
        now = self._now_ms()
        for name, value in mapping.items():
            blob[name] = {"value": value, "created_at": now}
        self._guard_size(blob)
        self._blobs[user_id] = blob

    async def delete_secret(self, user_id: str, name: str) -> bool:
        blob = self._blob(user_id)
        if name not in blob:
            return False
        del blob[name]
        return True


_STORE_ENV = "LHA_SECRET_BACKEND"
_STORE: SecretStore | None = None


def get_secret_store() -> SecretStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    backend = os.environ.get(_STORE_ENV, "secretmanager").strip().lower()
    if backend in ("secretmanager", "gcp"):
        from google.cloud import secretmanager

        _STORE = SecretManagerStore(
            client=secretmanager.SecretManagerServiceAsyncClient(),
            project_id=os.environ["GOOGLE_CLOUD_PROJECT"],
        )
    elif backend == "memory":
        _STORE = InMemorySecretStore()
    else:
        raise ValueError(
            f"unknown {_STORE_ENV}={backend!r}; expected 'secretmanager' or 'memory'"
        )
    return _STORE


def set_secret_store(store: SecretStore | None) -> None:
    global _STORE
    _STORE = store


def reset_secret_store() -> None:
    global _STORE
    _STORE = None
