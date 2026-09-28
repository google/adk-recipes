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

"""Resolve the active user's secrets for env injection into sandbox commands."""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# When a routine turn is active, holds the routine's declared secret names so
# secret_env() (called by terminal/process) injects only those — never the
# user's full surface. None = unscoped (normal interactive turns).
_routine_secret_scope: contextvars.ContextVar[frozenset[str] | None] = (
    contextvars.ContextVar("routine_secret_scope", default=None)
)


def set_routine_secret_scope(names: Iterable[str]) -> contextvars.Token:
    """Restrict secret_env() to ``names`` for the current async context."""
    return _routine_secret_scope.set(
        frozenset(n for n in names if isinstance(n, str))
    )


def reset_routine_secret_scope(token: contextvars.Token) -> None:
    _routine_secret_scope.reset(token)


# Holds the Gemini Enterprise-forwarded end-user Google access token for the
# current turn. secret_env() overlays it as CLOUDSDK_AUTH_ACCESS_TOKEN so the
# sandbox acts as the user. Request-scoped; never persisted to Secret Manager.
_delegated_google_token: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("delegated_google_token", default=None)
)


def set_delegated_google_token(token: str) -> contextvars.Token:
    return _delegated_google_token.set(token)


def reset_delegated_google_token(token: contextvars.Token) -> None:
    _delegated_google_token.reset(token)


async def _resolve_secret_env(allow: set[str] | None) -> dict[str, str]:
    """Resolve the active user's secrets, optionally filtered to ``allow``.

    Identity comes from the active environment's ``owner`` (sandbox backend) or
    the request-scoped user_id contextvar (local backend). ``allow=None`` returns
    every stored secret for the user; a set restricts the result to those names.
    Never raises.
    """
    from app.auth.identity import get_user_id_from_context
    from app.environment_context import active_environment
    from app.secrets.store import get_secret_store

    owner: str | None = None
    try:
        owner = getattr(active_environment(), "owner", None)
    except RuntimeError:
        owner = None
    if not owner:
        owner = get_user_id_from_context()
    if not owner:
        return {}

    try:
        result: dict[str, str] = dict(await get_secret_store().get_all(owner))
        # OAuth tokens live in the same per-user store as user secrets.
        delegated = _delegated_google_token.get()
        if delegated:
            result["CLOUDSDK_AUTH_ACCESS_TOKEN"] = delegated
        if allow is not None:
            result = {k: v for k, v in result.items() if k in allow}
        return result
    except Exception:
        logger.exception("secret_env: failed to resolve secrets for user")
        return {}


async def secret_env() -> dict[str, str]:
    """Return ``{name: value}`` for the current user, or ``{}`` on any failure.

    Never raises — secret resolution must not break command execution. When a
    routine secret scope is active, only the scoped names are returned.
    """
    scope = _routine_secret_scope.get()
    return await _resolve_secret_env(None if scope is None else set(scope))


async def scoped_secret_env(declared: Iterable[str]) -> dict[str, str]:
    """Like ``secret_env`` but restricted to the explicitly ``declared`` names.

    Used by scheduled routines: a routine receives ONLY the secrets its manifest
    declares (and only if the active capabilities permit them) — never the user's
    full secret surface.
    """
    names = {n for n in declared if isinstance(n, str)}
    return await _resolve_secret_env(names)
