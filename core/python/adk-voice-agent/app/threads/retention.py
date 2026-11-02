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

"""Retention cleanup (`RETENTION_DAYS`) for finished threads, ADK sessions, and conversation memories."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.threads.registry import ThreadRegistry

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
RETENTION_S = RETENTION_DAYS * 86400
CLEANUP_INTERVAL_S = 86400.0


async def purge_expired_threads(
    registry: ThreadRegistry | None = None, *, now: float | None = None
) -> int:
    """Delete finished threads and ADK sessions older than `RETENTION_DAYS`."""
    from app.app_utils.services import purge_expired_sessions
    from app.threads.registry import get_thread_registry

    registry = registry or get_thread_registry()
    cutoff = (now if now is not None else time.time()) - RETENTION_S
    count = await registry.purge_finished_before(cutoff)
    try:
        await purge_expired_sessions(cutoff)
    except Exception:
        logger.warning("Session retention cleanup failed", exc_info=True)
    if count:
        logger.info(
            "Deleted %d thread(s) finished more than %d days ago.",
            count,
            RETENTION_DAYS,
        )
    return count


async def retention_loop(interval_s: float = CLEANUP_INTERVAL_S) -> None:
    while True:
        try:
            await purge_expired_threads()
        except Exception:
            logger.warning("Thread retention cleanup failed", exc_info=True)
        await asyncio.sleep(interval_s)
