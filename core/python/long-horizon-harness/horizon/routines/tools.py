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

"""Model-callable routine tool: test / create (HITL-gated) / list / cancel.

test dry-runs the task once under the routine's real isolation (run_once) without
scheduling anything. create proposes a manifest, gates on user confirmation, and
on approval writes ``.lha/routines/<id>.yaml`` (env interface) AND registers the
RoutineStore row that the fire path consumes. The declared ``secrets`` are the
routine's blast-radius bound.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal

from horizon.environment_context import active_environment
from horizon.routines.manifest import (
    DEFAULT_DELIVERY,
    ROUTINE_OVERLAY_DIR,
    write_routine_via_env,
)
from horizon.routines.run_once import run_routine_once
from horizon.scheduler.cron import is_valid_cron, next_cron_fire
from horizon.scheduler.routine_store import RoutineRow, get_routine_store
from horizon.tools._hitl import request_user_confirmation

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return s or "routine"


def _identity(tool_context: Any) -> tuple[str, str] | None:
    inv = getattr(tool_context, "_invocation_context", None)
    if inv is None:
        return None
    user_id = getattr(inv, "user_id", "") or ""
    app_name = getattr(inv, "app_name", "") or ""
    if not user_id or not app_name:
        return None
    return user_id, app_name


async def _create(
    *, name: str, schedule: str, task: str, secrets, delivery, tool_context
) -> dict[str, Any]:
    if not (name and schedule and task):
        return {
            "success": False,
            "error": "create requires name, schedule, task",
        }
    if not is_valid_cron(schedule):
        return {
            "success": False,
            "error": f"invalid cron schedule {schedule!r} (e.g. '0 8 * * *')",
        }
    ident = _identity(tool_context)
    if ident is None:
        return {"success": False, "error": "no active invocation context"}
    user_id, app_name = ident

    routine_id = _slug(name)
    body = {
        "name": name,
        "schedule": schedule,
        "task": task,
        "secrets": list(secrets or []),
        "delivery": delivery or DEFAULT_DELIVERY,
    }

    tc = request_user_confirmation(
        tool_context,
        hint=f"Schedule routine '{name}' ({schedule})? It runs unattended in an "
        f"isolated sandbox with only these secrets: {body['secrets'] or 'none'}.",
        payload={"kind": "routine", "manifest": body, "routine_id": routine_id},
    )
    if tc is None:
        return {
            "success": True,
            "status": "awaiting_user_response",
            "routine_id": routine_id,
        }
    if not getattr(tc, "confirmed", False):
        return {
            "success": False,
            "status": "declined",
            "error": "user declined",
        }

    # confirmed → backend writes the manifest + registers the schedule row
    await write_routine_via_env(
        active_environment(), body, routine_id=routine_id
    )
    now = datetime.now(UTC)
    next_at = next_cron_fire(schedule, now)
    await get_routine_store().add(
        RoutineRow(
            id=routine_id,
            user_id=user_id,
            app_name=app_name,
            schedule=schedule,
            task=task,
            secrets=tuple(body["secrets"]),
            delivery=body["delivery"],
            next_fire_at=next_at,
            created_at=now,
        )
    )
    return {
        "success": True,
        "id": routine_id,
        "next_fire_at": next_at.isoformat(),
    }


async def _test(
    *, name: str, task: str, secrets, tool_context
) -> dict[str, Any]:
    if not task:
        return {"success": False, "error": "test requires task"}
    ident = _identity(tool_context)
    if ident is None:
        return {"success": False, "error": "no active invocation context"}
    user_id, _ = ident
    return await run_routine_once(
        routine_id=_slug(name),
        owner=user_id,
        task=task,
        secrets=list(secrets or []),
    )


async def _list(tool_context) -> dict[str, Any]:
    ident = _identity(tool_context)
    if ident is None:
        return {"success": False, "error": "no active invocation context"}
    user_id, _ = ident
    rows = await get_routine_store().list_for_user(user_id)
    return {
        "success": True,
        "routines": [
            {
                "id": r.id,
                "schedule": r.schedule,
                "next_fire_at": r.next_fire_at.isoformat(),
            }
            for r in rows
        ],
    }


async def _cancel(id: str, tool_context: Any) -> dict[str, Any]:
    if not id:
        return {"success": False, "error": "cancel requires id"}
    ident = _identity(tool_context)
    if ident is None:
        return {"success": False, "error": "no active invocation context"}
    user_id, _ = ident
    ok = await get_routine_store().cancel(id, user_id)
    if not ok:
        return {"success": False, "error": "no such routine"}

    env = active_environment()
    manifest_path = env.working_dir / ROUTINE_OVERLAY_DIR / f"{id}.yaml"
    try:
        await env.delete_file(manifest_path)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        logger.debug(
            "Failed to delete manifest for canceled routine %s: %s", id, exc
        )

    return {"success": True, "id": id}


async def routine(
    action: Literal["create", "test", "list", "cancel"],
    *,
    name: str | None = None,
    schedule: str | None = None,
    task: str | None = None,
    secrets: list[str] | None = None,
    delivery: str | None = None,
    id: str | None = None,
    tool_context: Any,
) -> dict[str, Any]:
    """Create, test, list, or cancel a scheduled routine — a recurring
    task that runs UNATTENDED in a fresh isolated sandbox with only the
    secrets you declare.

    - `test`: run it ONCE now under real isolation (own sandbox, only
      declared `secrets`), without scheduling. Requires `task`; use the
      same `name`/`secrets` you intend to schedule. ALWAYS test before
      `create`. Blocks until done; returns `{success, status, output,
      ...}`.
    - `create`: requires `name`, `schedule` (5-field cron), `task`.
      Optional `secrets` (only ones the run receives) and `delivery`
      (default `report_back`). Asks the user to confirm; the first call
      returns status="awaiting_user_response" — wait for the user, don't
      claim it's scheduled until success: true with an `id`.
    - `list` / `cancel`: the caller's routines, by `id` for cancel.
    """
    if action == "test":
        return await _test(
            name=name or "",
            task=task or "",
            secrets=secrets,
            tool_context=tool_context,
        )
    if action == "create":
        return await _create(
            name=name or "",
            schedule=schedule or "",
            task=task or "",
            secrets=secrets,
            delivery=delivery,
            tool_context=tool_context,
        )
    if action == "list":
        return await _list(tool_context)
    if action == "cancel":
        return await _cancel(id or "", tool_context)
    return {"success": False, "error": f"unknown action {action!r}"}


__all__ = ["routine"]
