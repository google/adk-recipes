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

"""Agent callbacks: thread hydration at the start of a live session."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.app_utils.user_context import warm_user_context
from app.auth.identity import acting_user_id
from app.memory.ingest import remember_on_close
from app.threads.registry import get_thread_registry
from app.voice.briefing import (
    collect_missed_milestones,
    collect_profile,
    collect_work,
)
from app.voice.continuity import collect_carryover
from app.voice.thread_updates import get_thread_updates
from app.workers.providers.reaper import voice_session_opened

logger = logging.getLogger(__name__)


async def hydrate_threads(callback_context: Any) -> None:
    """Preload user context, attach live thread updates, and gather session briefings."""
    await warm_user_context()
    inv_ctx = getattr(callback_context, "_invocation_context", None)
    live_queue = getattr(inv_ctx, "live_request_queue", None)
    if live_queue is not None:
        session_task = asyncio.current_task()
        session = getattr(inv_ctx, "session", None)
        get_thread_updates().attach(
            get_thread_registry(), live_queue, session_task
        )
        remember_on_close(acting_user_id(), session, session_task)
        voice_session_opened(acting_user_id(), session_task)
    briefing, _, _, work = await asyncio.gather(
        collect_missed_milestones(),
        collect_profile(),
        collect_carryover(inv_ctx),
        collect_work(),
        return_exceptions=True,
    )
    if isinstance(briefing, BaseException):
        logger.warning("Could not load the thread briefing", exc_info=briefing)
    if isinstance(work, BaseException):
        logger.warning("Could not load the user's projects", exc_info=work)
