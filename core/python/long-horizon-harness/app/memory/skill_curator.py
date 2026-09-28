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

"""Skill curator pass — inline promotion/review based on session telemetry.

Runs the promotion logic inline as an ``after_agent_callback`` using simple
numeric thresholds:

* ``MIN_MANAGES_FOR_PROMOTION = 3`` — three successful edits to the
  same skill in one session is a strong "the agent is actively shaping
  this skill" signal (each ``skill`` mutate action bumps the skill's
  patch count).
* ``MIN_VIEWS_FOR_REVIEW = 5`` — five reads with zero edits flags a
  skill that's load-bearing context but never refined ("view-heavy,
  patch-light").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from google.adk.memory import BaseMemoryService

from app.memory._writer import entry_exists, write_memory_event
from app.memory.skill_telemetry import SKILL_TELEMETRY_STATE_KEY

MIN_MANAGES_FOR_PROMOTION = 3
MIN_VIEWS_FOR_REVIEW = 5

CURATOR_MARKER = "[skill_curator]"


def _promotion_text(skill_name: str, manages: int, session_id: str) -> str:
    return (
        f"{CURATOR_MARKER} Skill '{skill_name}' is reliable "
        f"({manages} successful edits in session {session_id})"
    )


def _review_text(skill_name: str) -> str:
    return (
        f"{CURATOR_MARKER} Skill '{skill_name}' is read often but never "
        "edited — review for promotion to docs"
    )


async def _write_curator_entry(
    *,
    text: str,
    memory_service: BaseMemoryService,
    app_name: str,
    user_id: str,
    session_id: str,
) -> None:
    if await entry_exists(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        text=text,
    ):
        return
    await write_memory_event(
        text=text,
        scope="skill_curator",
        invocation_id="skill_curator",
        author="system",
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )


async def run_skill_curator(
    *,
    telemetry: Mapping[str, Mapping[str, Any]] | None,
    memory_service: BaseMemoryService,
    app_name: str,
    user_id: str,
    session_id: str,
) -> None:
    """Walk telemetry once and write promotion/review entries to memory."""
    if not telemetry:
        return
    for skill_name, rec in telemetry.items():
        if not isinstance(skill_name, str) or not skill_name:
            continue
        if not isinstance(rec, Mapping):
            continue
        try:
            views = int(rec.get("views", 0) or 0)
            manages = int(rec.get("manages", 0) or 0)
        except (TypeError, ValueError):
            continue

        if manages >= MIN_MANAGES_FOR_PROMOTION:
            await _write_curator_entry(
                text=_promotion_text(skill_name, manages, session_id),
                memory_service=memory_service,
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
        elif views >= MIN_VIEWS_FOR_REVIEW and manages == 0:
            await _write_curator_entry(
                text=_review_text(skill_name),
                memory_service=memory_service,
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )


async def skill_curator_callback(callback_context: Any) -> None:
    """``after_agent_callback`` adapter for ``run_skill_curator``."""
    invocation_context = callback_context._invocation_context
    memory_service = getattr(invocation_context, "memory_service", None)
    if memory_service is None:
        return None
    telemetry = callback_context.state.get(SKILL_TELEMETRY_STATE_KEY)
    if not telemetry:
        return None
    await run_skill_curator(
        telemetry=telemetry,
        memory_service=memory_service,
        app_name=invocation_context.app_name,
        user_id=invocation_context.user_id,
        session_id=invocation_context.session.id,
    )
    return None
