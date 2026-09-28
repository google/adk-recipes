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
"""POST /feedback — the human feedback button's durable capture path."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, FastAPI

from app.auth import current_user_id
from app.feedback.context import build_redacted_context
from app.feedback.models import Feedback
from app.feedback.sink import emit_feedback_record
from app.infrastructure.constants import APP_NAME

logger = logging.getLogger(__name__)


async def _feedback_context_summary(
    runner: Any, user_id: str, session_id: str
) -> str:
    if runner is None:
        return ""
    try:
        session = await runner.session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
    except Exception:
        logger.exception(
            "feedback: get_session failed for %s/%s", user_id, session_id
        )
        return ""
    return build_redacted_context(session) if session else ""


def attach_feedback_routes(app: FastAPI, *, runner: Any) -> None:
    @app.post("/feedback")
    async def collect_feedback(
        feedback: Feedback, user_id: str = Depends(current_user_id)
    ) -> dict[str, str]:
        record: dict[str, Any] = {
            **feedback.model_dump(),
            "user_id": user_id,
            "origin": "user",
        }
        if feedback.include_context:
            record["context_summary"] = await _feedback_context_summary(
                runner, user_id, feedback.session_id
            )
        # emit_feedback_record is resilient (logs a fallback copy and never
        # raises), but guard here too so a future regression can never 500 the
        # user out of their typed feedback.
        try:
            emit_feedback_record(record)
        except Exception:
            logger.exception(
                "feedback: emit raised despite resilient sink; record=%s",
                record,
            )
        return {"status": "success"}
