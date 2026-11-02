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

"""Per-user connected-app tokens: `.env` locally, Secret Manager remotely with stale-while-revalidate caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from app.auth.env_store import ENV_KEY_PATTERN, persist_env_vars

logger = logging.getLogger(__name__)

USER_SECRET_PREFIX = "voice-agent-user-"  # noqa: S105 - a secret name prefix, not a secret
#: Secret Manager's payload limit.
MAX_BLOB_BYTES = 64 * 1024
#: How long a user's tokens are served from memory before Secret Manager is re-read.
CACHE_TTL_S = 30.0
#: How long past the TTL a cached copy is still served while it refreshes in the
#: background. Older than this, a read waits for Secret Manager.
MAX_STALE_S = 600.0


class CredentialStore(Protocol):
    """Per-user connected-app token store keyed by env var name."""

    per_user: bool

    def get_all(self, user_id: str) -> dict[str, str]: ...

    def get(self, user_id: str, key: str) -> str: ...

    def set_many(
        self, user_id: str, updates: Mapping[str, str | None]
    ) -> bool: ...

    def delete(self, user_id: str, keys: Iterable[str]) -> bool: ...

    def invalidate(self, user_id: str) -> None: ...


def _validate_keys(keys: Iterable[str]) -> None:
    if invalid := [k for k in keys if not ENV_KEY_PATTERN.fullmatch(k)]:
        raise ValueError(
            f"Invalid environment variable name(s): {', '.join(invalid)}"
        )


class EnvCredentialStore:
    per_user = False

    def get_all(self, user_id: str) -> dict[str, str]:
        from app.auth.providers import user_credential_env_keys

        return {
            key: value
            for key in user_credential_env_keys()
            if (value := os.getenv(key, "").strip())
        }

    def get(self, user_id: str, key: str) -> str:
        return os.getenv(key, "").strip()

    def set_many(self, user_id: str, updates: Mapping[str, str | None]) -> bool:
        return persist_env_vars(dict(updates)) if updates else True

    def delete(self, user_id: str, keys: Iterable[str]) -> bool:
        return self.set_many(user_id, dict.fromkeys(keys))

    def invalidate(self, user_id: str) -> None:
        pass


def user_secret_prefix() -> str:
    deployment = os.getenv("COMPUTER_DEPLOYMENT", "").strip()
    return f"{deployment}-user-" if deployment else USER_SECRET_PREFIX


def user_secret_id(user_id: str) -> str:
    """The Secret Manager secret id holding `user_id`'s tokens (no PII in the name)."""
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return f"{user_secret_prefix()}{digest}"


class SecretManagerCredentialStore:
    per_user = True
    secret_kind = "user-credentials"  # noqa: S105 - a label, not a secret
    destroy_replaced = True

    def __init__(
        self,
        *,
        client: Any = None,
        project_id: str | None = None,
        ttl_s: float = CACHE_TTL_S,
        max_stale_s: float = MAX_STALE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._project_id = project_id
        self._ttl_s = ttl_s
        self._max_stale_s = max_stale_s
        self._clock = clock
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        # Bumped on write/invalidation so an older in-flight fetch cannot overwrite the cache.
        self._generation: dict[str, int] = {}
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @property
    def project_id(self) -> str:
        project = (
            self._project_id or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
        ).strip()
        if not project:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT is not set, so per-user credentials have "
                "nowhere to live in Secret Manager."
            )
        return project

    def _secret_id(self, user_id: str) -> str:
        return user_secret_id(user_id)

    def _secret_name(self, user_id: str) -> str:
        return f"projects/{self.project_id}/secrets/{self._secret_id(user_id)}"

    def _fetch(self, user_id: str) -> tuple[dict[str, str], str | None]:
        from google.api_core.exceptions import NotFound

        try:
            resp = self.client.access_secret_version(
                request={
                    "name": f"{self._secret_name(user_id)}/versions/latest"
                }
            )
        except NotFound:
            return {}, None
        try:
            blob = json.loads(resp.payload.data)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "Credential secret is not valid JSON; treating as empty"
            )
            blob = {}
        tokens = (
            {str(k): str(v) for k, v in blob.items() if isinstance(v, str)}
            if isinstance(blob, dict)
            else {}
        )
        return tokens, getattr(resp, "name", None) or None

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._cache.pop(user_id, None)
            self._generation[user_id] = self._generation.get(user_id, 0) + 1

    def _remember(
        self, user_id: str, tokens: dict[str, str], generation: int | None
    ) -> None:
        with self._lock:
            current = self._generation.get(user_id, 0)
            if generation is not None and generation != current:
                return
            if generation is None:
                self._generation[user_id] = current + 1
            self._cache[user_id] = (self._clock() + self._ttl_s, dict(tokens))

    def _load(self, user_id: str) -> dict[str, str]:
        generation = self._generation.get(user_id, 0)
        tokens, _ = self._fetch(user_id)
        self._remember(user_id, tokens, generation)
        return dict(tokens)

    def is_fresh(self, user_id: str) -> bool:
        hit = self._cache.get(user_id)
        return hit is not None and hit[0] > self._clock()

    def get_all(self, user_id: str) -> dict[str, str]:
        now = self._clock()
        hit = self._cache.get(user_id)
        if hit is not None:
            expires_at, tokens = hit
            if expires_at > now:
                return dict(tokens)
            if expires_at + self._max_stale_s > now:
                self._refresh_in_background(user_id)
                return dict(tokens)
        return self._load(user_id)

    def _refresh_in_background(self, user_id: str) -> None:
        with self._lock:
            if user_id in self._refreshing:
                return
            self._refreshing.add(user_id)

        def refresh() -> None:
            try:
                self._load(user_id)
            except Exception as exc:
                logger.warning("Could not refresh cached credentials: %s", exc)
            finally:
                with self._lock:
                    self._refreshing.discard(user_id)

        threading.Thread(
            target=refresh, name="credential-refresh", daemon=True
        ).start()

    def get(self, user_id: str, key: str) -> str:
        return self.get_all(user_id).get(key, "").strip()

    def set_many(self, user_id: str, updates: Mapping[str, str | None]) -> bool:
        if not updates:
            return True
        _validate_keys(updates)
        with self._write_lock:
            current, version = self._fetch(user_id)
            merged = dict(current)
            for key, value in updates.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            if merged == current and version is not None:
                self._remember(user_id, current, None)
                return True
            data = json.dumps(merged, sort_keys=True).encode("utf-8")
            if len(data) > MAX_BLOB_BYTES:
                raise ValueError(
                    f"Credentials for this user exceed {MAX_BLOB_BYTES} bytes."
                )
            self._ensure_secret(user_id)
            self.client.add_secret_version(
                request={
                    "parent": self._secret_name(user_id),
                    "payload": {"data": data},
                }
            )
            self._remember(user_id, merged, None)
            if version and self.destroy_replaced:
                self._destroy(version)
        return True

    def delete(self, user_id: str, keys: Iterable[str]) -> bool:
        return self.set_many(user_id, dict.fromkeys(keys))

    def _ensure_secret(self, user_id: str) -> None:
        from google.api_core.exceptions import AlreadyExists

        try:
            self.client.create_secret(
                request={
                    "parent": f"projects/{self.project_id}",
                    "secret_id": self._secret_id(user_id),
                    "secret": {
                        "replication": {"automatic": {}},
                        "labels": {
                            "app": "adk-voice-agent",
                            "kind": self.secret_kind,
                        },
                    },
                }
            )
        except AlreadyExists:
            pass

    def _destroy(self, version: str) -> None:
        from google.api_core.exceptions import GoogleAPICallError

        try:
            self.client.destroy_secret_version(request={"name": version})
        except GoogleAPICallError as exc:
            logger.warning(
                "Could not destroy replaced credential version: %s", exc
            )


class StoreBackend:
    AUTO = "auto"
    ENV = "env"
    SECRET_MANAGER = "secret_manager"  # noqa: S105 - a backend name, not a secret


_override: CredentialStore | None = None
_backends: dict[str, CredentialStore] = {}
_secret_manager_client: Any = None
_secret_manager_project: str | None = None


def credential_backend() -> str:
    raw = (
        (os.getenv("VOICE_AGENT_CREDENTIAL_STORE") or StoreBackend.AUTO)
        .strip()
        .lower()
    )
    if raw == StoreBackend.AUTO:
        from app.auth.providers import is_remote_deployment

        return (
            StoreBackend.SECRET_MANAGER
            if is_remote_deployment()
            else StoreBackend.ENV
        )
    if raw not in (StoreBackend.ENV, StoreBackend.SECRET_MANAGER):
        raise ValueError(
            f"VOICE_AGENT_CREDENTIAL_STORE={raw!r} is not one of auto, env, secret_manager."
        )
    return raw


def get_credential_store() -> CredentialStore:
    if _override is not None:
        return _override
    backend = credential_backend()
    if backend not in _backends:
        _backends[backend] = (
            SecretManagerCredentialStore(
                client=_secret_manager_client,
                project_id=_secret_manager_project,
            )
            if backend == StoreBackend.SECRET_MANAGER
            else EnvCredentialStore()
        )
    return _backends[backend]


def set_credential_store(store: CredentialStore | None) -> None:
    global _override
    _override = store
    _backends.clear()


def set_secret_manager_client(
    client: Any, project_id: str | None = None
) -> None:
    global _secret_manager_client, _secret_manager_project
    _secret_manager_client = client
    _secret_manager_project = project_id
    _backends.clear()


def credential_owner(store: CredentialStore | None = None) -> str:
    """Return the acting user, falling back to `dev_user_id()` only on the shared local store."""
    from app.auth.identity import acting_user_id, dev_user_id

    try:
        return acting_user_id()
    except RuntimeError:
        if (store or get_credential_store()).per_user:
            raise
        return dev_user_id()


def credential_owner_or_none() -> str | None:
    try:
        return credential_owner()
    except RuntimeError:
        return None


async def prefetch_user_credentials(user_id: str | None = None) -> None:
    """Warm the user's tokens in a worker thread so sync reads on the event loop do not block."""
    store = get_credential_store()
    if user_id is None:
        user_id = credential_owner_or_none()
        if user_id is None:
            return
    is_fresh = getattr(store, "is_fresh", None)
    if is_fresh is not None and is_fresh(user_id):
        return
    await asyncio.to_thread(store.get_all, user_id)


async def reload_user_credentials() -> dict[str, str]:
    store = get_credential_store()
    user_id = credential_owner(store)
    store.invalidate(user_id)
    return await asyncio.to_thread(store.get_all, user_id)


def user_credential(key: str | None) -> str:
    if not key:
        return ""
    store = get_credential_store()
    return store.get(credential_owner(store), key)


def save_user_credentials(updates: Mapping[str, str | None]) -> bool:
    store = get_credential_store()
    return store.set_many(credential_owner(store), updates)
