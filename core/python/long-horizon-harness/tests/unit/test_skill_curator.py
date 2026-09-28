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

"""Skill curator pass — promotes/flags skills based on per-session telemetry.

Pins the contract for ``app.memory.skill_curator``:

* ``run_skill_curator(*, telemetry, memory_service, app_name, user_id,
  session_id)`` reads the telemetry dict, writes Memory Bank entries for
  skills that crossed promotion / review thresholds, and is idempotent
  (re-running with the same telemetry adds nothing new).

* ``skill_curator_callback(callback_context)`` is the ADK
  ``after_agent_callback`` adapter that pulls the telemetry off
  ``session.state`` and delegates to ``run_skill_curator``.

The promotion step runs inline as an ``after_agent_callback`` at end of
invocation; a background/scheduled curator is out of scope.

Thresholds (see ``app/memory/skill_curator.py`` constants):
* ``MIN_MANAGES_FOR_PROMOTION = 3`` — enough successful edits in one
  session to call a skill ``reliable``.
* ``MIN_VIEWS_FOR_REVIEW = 5`` — frequently read but never edited;
  surface for human review.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.memory import InMemoryMemoryService

APP_NAME = "curator_app"
USER_ID = "curator_user"
SESSION_ID = "session_xyz"


async def _all_memory_text(
    service: InMemoryMemoryService, query: str
) -> list[str]:
    response = await service.search_memory(
        app_name=APP_NAME, user_id=USER_ID, query=query
    )
    out: list[str] = []
    for memory in response.memories:
        if not memory.content or not memory.content.parts:
            continue
        for part in memory.content.parts:
            if part.text:
                out.append(part.text)
    return out


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_thresholds_and_entrypoints():
    from app.memory.skill_curator import (
        MIN_MANAGES_FOR_PROMOTION,
        MIN_VIEWS_FOR_REVIEW,
        run_skill_curator,
        skill_curator_callback,
    )

    assert MIN_MANAGES_FOR_PROMOTION == 3
    assert MIN_VIEWS_FOR_REVIEW == 5
    assert callable(run_skill_curator)
    assert callable(skill_curator_callback)


# =============================================================================
# Promotion: skill has >= 3 manages
# =============================================================================


@pytest.mark.asyncio
class TestPromotion:
    async def test_three_manages_promotes(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "git-rebase": {
                "views": 0,
                "manages": 3,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        hits = await _all_memory_text(fake_memory_service, "git-rebase")
        promoted = [h for h in hits if "reliable" in h and "git-rebase" in h]
        assert len(promoted) == 1, f"expected one promotion entry, got {hits!r}"
        assert SESSION_ID in promoted[0]

    async def test_two_manages_does_not_promote(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "git-rebase": {
                "views": 1,
                "manages": 2,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        hits = await _all_memory_text(fake_memory_service, "git-rebase")
        assert hits == []

    async def test_many_manages_still_one_entry(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "git-rebase": {
                "views": 0,
                "manages": 50,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        hits = await _all_memory_text(fake_memory_service, "git-rebase")
        promoted = [h for h in hits if "reliable" in h]
        assert len(promoted) == 1


# =============================================================================
# Review: views >= 5 AND zero manages
# =============================================================================


@pytest.mark.asyncio
class TestReview:
    async def test_five_views_zero_manages_flags_review(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "docs-reader": {
                "views": 5,
                "manages": 0,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        hits = await _all_memory_text(fake_memory_service, "docs-reader")
        flagged = [
            h for h in hits if "docs-reader" in h and "never edited" in h
        ]
        assert len(flagged) == 1, f"expected one review entry, got {hits!r}"

    async def test_five_views_with_manages_does_not_flag_review(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "active-skill": {
                "views": 5,
                "manages": 1,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        hits = await _all_memory_text(fake_memory_service, "active-skill")
        flagged = [h for h in hits if "never edited" in h]
        assert flagged == []

    async def test_four_views_zero_manages_does_not_flag(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "low-views": {
                "views": 4,
                "manages": 0,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        hits = await _all_memory_text(fake_memory_service, "low-views")
        assert hits == []


# =============================================================================
# Idempotency: re-running with the same telemetry adds no duplicates
# =============================================================================


@pytest.mark.asyncio
class TestIdempotency:
    async def test_promote_is_idempotent(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "git-rebase": {
                "views": 0,
                "manages": 5,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        for _ in range(3):
            await run_skill_curator(
                telemetry=telem,
                memory_service=fake_memory_service,
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=SESSION_ID,
            )

        hits = await _all_memory_text(fake_memory_service, "git-rebase")
        promoted = [h for h in hits if "reliable" in h]
        assert len(promoted) == 1, (
            f"three runs of the same telemetry must not duplicate the entry; "
            f"got {promoted!r}"
        )

    async def test_review_is_idempotent(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "docs-reader": {
                "views": 7,
                "manages": 0,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        for _ in range(3):
            await run_skill_curator(
                telemetry=telem,
                memory_service=fake_memory_service,
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=SESSION_ID,
            )

        hits = await _all_memory_text(fake_memory_service, "docs-reader")
        flagged = [h for h in hits if "never edited" in h]
        assert len(flagged) == 1


# =============================================================================
# Multi-skill + below-threshold mixed
# =============================================================================


@pytest.mark.asyncio
class TestMultiSkill:
    async def test_only_qualifying_skills_emit_entries(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        telem = {
            "promoted": {
                "views": 1,
                "manages": 3,
                "last_used": "2026-05-19T10:00:00+00:00",
            },
            "flagged": {
                "views": 6,
                "manages": 0,
                "last_used": "2026-05-19T10:00:00+00:00",
            },
            "ignored": {
                "views": 1,
                "manages": 1,
                "last_used": "2026-05-19T10:00:00+00:00",
            },
        }
        await run_skill_curator(
            telemetry=telem,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

        promoted_hits = await _all_memory_text(fake_memory_service, "promoted")
        flagged_hits = await _all_memory_text(fake_memory_service, "flagged")
        ignored_hits = await _all_memory_text(fake_memory_service, "ignored")

        assert any("reliable" in h for h in promoted_hits)
        assert any("never edited" in h for h in flagged_hits)
        # The skill literally named "ignored" must not appear in a curator
        # entry; semantic search may surface other text containing the word,
        # so we check specifically for entries that mention the skill name.
        assert not any(
            ("reliable" in h or "never edited" in h) and "'ignored'" in h
            for h in ignored_hits
        )


# =============================================================================
# Empty / missing telemetry: no-op
# =============================================================================


@pytest.mark.asyncio
class TestEmpty:
    async def test_empty_telemetry_is_noop(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        await run_skill_curator(
            telemetry={},
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )
        # Sanity-check: nothing showed up under any reasonable probe.
        for probe in ("reliable", "never edited", "skill"):
            hits = await _all_memory_text(fake_memory_service, probe)
            assert hits == []

    async def test_none_telemetry_is_noop(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import run_skill_curator

        await run_skill_curator(
            telemetry=None,
            memory_service=fake_memory_service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )


# =============================================================================
# Callback adapter: pulls state + identifiers off CallbackContext
# =============================================================================


@pytest.mark.asyncio
class TestCallback:
    async def test_callback_runs_curator_with_session_state(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import skill_curator_callback
        from app.memory.skill_telemetry import SKILL_TELEMETRY_STATE_KEY

        state = {
            SKILL_TELEMETRY_STATE_KEY: {
                "git-rebase": {
                    "views": 0,
                    "manages": 4,
                    "last_used": "2026-05-19T10:00:00+00:00",
                }
            }
        }
        callback_context = SimpleNamespace(
            state=state,
            _invocation_context=SimpleNamespace(
                memory_service=fake_memory_service,
                app_name=APP_NAME,
                user_id=USER_ID,
                session=SimpleNamespace(id=SESSION_ID),
            ),
        )

        result = await skill_curator_callback(callback_context)
        assert result is None

        hits = await _all_memory_text(fake_memory_service, "git-rebase")
        promoted = [h for h in hits if "reliable" in h]
        assert len(promoted) == 1

    async def test_callback_noop_without_memory_service(self):
        from app.memory.skill_curator import skill_curator_callback
        from app.memory.skill_telemetry import SKILL_TELEMETRY_STATE_KEY

        state = {
            SKILL_TELEMETRY_STATE_KEY: {
                "skill-x": {
                    "views": 0,
                    "manages": 10,
                    "last_used": "2026-05-19T10:00:00+00:00",
                }
            }
        }
        callback_context = SimpleNamespace(
            state=state,
            _invocation_context=SimpleNamespace(
                memory_service=None,
                app_name=APP_NAME,
                user_id=USER_ID,
                session=SimpleNamespace(id=SESSION_ID),
            ),
        )
        # Must not raise even when memory is unconfigured.
        result = await skill_curator_callback(callback_context)
        assert result is None

    async def test_callback_noop_without_telemetry(
        self, fake_memory_service: InMemoryMemoryService
    ):
        from app.memory.skill_curator import skill_curator_callback

        callback_context = SimpleNamespace(
            state={},
            _invocation_context=SimpleNamespace(
                memory_service=fake_memory_service,
                app_name=APP_NAME,
                user_id=USER_ID,
                session=SimpleNamespace(id=SESSION_ID),
            ),
        )
        result = await skill_curator_callback(callback_context)
        assert result is None
