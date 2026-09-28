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

"""Shared helpers for reading per-tenant ``.lha`` overlay files (permissions,
policies, exfil) through the right backend.

Under the local-dev backend an overlay is a host file; under the sandbox backend
it lives inside the sandbox and is reachable only via the async env interface
(``env.read_file``). Sandbox reads are cached, and the cache MUST be keyed by
sandbox identity — every sandbox shares the same ``/workspace`` working dir, so
keying on ``working_dir`` would serve one tenant's overlay to another on a shared
backend instance.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def is_local_env(env: Any) -> bool:
    """True when overlays are host files (local backend), False when they live
    behind the env interface (sandbox / other remote backends)."""
    return bool(getattr(env, "on_host_fs", False))


def overlay_cache_key(env: Any) -> str:
    """A tenant-unique key for caching an env's overlays.

    Uses the env's ``cache_identity()`` (sandbox name for the sandbox backend,
    working dir for local) — NOT a bare ``working_dir``, since every sandbox
    shares the literal ``/workspace`` and would collide across tenants.
    """
    identity = getattr(env, "cache_identity", None)
    if callable(identity):
        return identity()
    return f"workdir:{getattr(env, 'working_dir', '')}"


async def read_overlay_text(env: Any, path: Path) -> str:
    """Read an overlay file inside the sandbox via the env interface.

    Returns ``''`` when the file is absent (404 → ``FileNotFoundError``). Any
    other read error (permission/transport) is logged and degrades to ``''`` —
    the conservative built-in defaults still apply — rather than crashing the
    tool call. Use this only for read-only loads; a read-before-write (append)
    must not swallow errors, or it could clobber the existing file.
    """
    try:
        raw = await env.read_file(path)
    except FileNotFoundError:
        return ""
    except Exception as exc:  # permission / transport / 5xx
        logger.warning(
            "overlay read failed for %s: %s — falling back to defaults",
            path,
            exc,
        )
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="replace")
    return str(raw)


__all__ = ["is_local_env", "overlay_cache_key", "read_overlay_text"]
