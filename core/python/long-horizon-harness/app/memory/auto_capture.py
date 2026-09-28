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

"""after_agent_callback that flushes the just-finished turn into Memory Bank."""

from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext

from app.memory._throttle import try_claim

logger = logging.getLogger(__name__)


async def auto_capture_callback(callback_context: CallbackContext) -> None:
    """Hand the current session to the memory service, throttled per session.

    Without this the next session has nothing for ``PreloadMemoryTool`` to
    surface. Throttle keyed via :mod:`app.memory._throttle` keeps short
    user-turn bursts from re-flushing the same near-identical snapshot.
    """
    if callback_context._invocation_context.memory_service is None:
        return None
    if not try_claim(callback_context.state, "auto_capture"):
        return None
    try:
        await callback_context.add_session_to_memory()
    except Exception:
        logger.exception("auto_capture: add_session_to_memory failed")
    return None
