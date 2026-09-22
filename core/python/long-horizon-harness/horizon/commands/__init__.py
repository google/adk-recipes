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

"""Built-in slash commands available to every session.

These cover capabilities that belong to the *user's* affordance surface,
not the model's: forcing a memory rollup, swapping models, granting a
per-session HITL bypass, or refreshing the skill catalog. Each one is
intercepted by ``make_slash_command_dispatcher`` before the model runs,
so the model never sees the ``/cmd`` turn.

``/reload`` shares its callable with the model-facing ``reload`` tool,
so typing ``/reload`` and asking the model to ``reload`` run identical
code paths.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from horizon.guardrails.permission_rules import APPROVAL_MODE_STATE_KEY
from horizon.guardrails.policy_grants import (
    POLICY_GRANTS_STATE_KEY,
    policy_grant,
)
from horizon.memory import dream_review
from horizon.models import (
    MODEL_REGISTRY,
    SELECTED_MODEL_STATE_KEY,
)
from horizon.models.selector import _resolve as _resolve_model
from horizon.tools import names
from horizon.tools.skill_reload import (
    BOUND_SKILLS_STATE_KEY,
    bound_skill_catalog,
    resync_and_refresh,
)

logger = logging.getLogger(__name__)

SlashHandler = Callable[[str, Any], Awaitable[str]]

BUILTIN_COMMAND_REGISTRY: dict[str, SlashHandler] = {}


def register(name: str) -> Callable[[SlashHandler], SlashHandler]:
    def decorator(fn: SlashHandler) -> SlashHandler:
        BUILTIN_COMMAND_REGISTRY[name] = fn
        return fn

    return decorator


@register("dream-review")
async def _dream_review(args: str, callback_context: Any) -> str:
    invocation_context = getattr(callback_context, "_invocation_context", None)
    if invocation_context is None:
        return "/dream-review: no active invocation context"
    result = await dream_review._run_dream_review_for_user(
        memory_service=getattr(invocation_context, "memory_service", None),
        app_name=getattr(invocation_context, "app_name", "") or "",
        user_id=getattr(invocation_context, "user_id", "") or "",
        session_service=getattr(invocation_context, "session_service", None),
    )
    if result.get("success"):
        n = result.get("sessions_reviewed", 0)
        return f"dream-review done — profile consolidated from {n} session(s)"
    return f"dream-review skipped: {result.get('reason', 'unknown')}"


async def reload(tool_context: Any = None) -> dict[str, Any]:
    """Refresh the skill catalog for the active session.

    Exposed both as a model-facing tool (``reload``) and as the
    ``/reload`` slash command.
    """
    diff = await resync_and_refresh()
    if diff is None:
        return {"skills_refreshed": False, "reason": "no skill toolset bound"}
    state = getattr(tool_context, "state", None)
    if state is not None:
        state[BOUND_SKILLS_STATE_KEY] = bound_skill_catalog()
    return {
        "skills_refreshed": True,
        "loaded": diff["loaded"],
        "removed": diff["removed"],
        "skills_total": diff["total"],
    }


@register("reload")
async def _reload(args: str, callback_context: Any) -> str:
    invocation_context = getattr(callback_context, "_invocation_context", None)
    user_id = getattr(invocation_context, "user_id", None)
    if user_id:
        from horizon.secrets import get_secret_store

        try:
            get_secret_store().invalidate(user_id)
        except Exception:
            logger.exception("/reload: secret cache invalidation failed")

    result = await reload(tool_context=callback_context)
    if not result.get("skills_refreshed"):
        return f"/reload: {result.get('reason', 'no-op')}"
    loaded = result.get("loaded") or []
    removed = result.get("removed") or []
    parts = [f"skills: {result.get('skills_total', '?')}"]
    if loaded:
        parts.append(f"+{', '.join(loaded)}")
    if removed:
        parts.append(f"-{', '.join(removed)}")
    return "reloaded · " + " · ".join(parts)


@register("model")
async def _model(args: str, callback_context: Any) -> str:
    state = getattr(callback_context, "state", None)
    name = args.strip()

    if not name:
        current = _resolve_model(state if isinstance(state, dict) else None)
        available = ", ".join(sorted(MODEL_REGISTRY))
        return f"current: {current}\navailable: {available}"

    if name not in MODEL_REGISTRY:
        available = ", ".join(sorted(MODEL_REGISTRY))
        return f"unknown model {name!r}; available: {available}"

    if state is None:
        return f"/model {name}: no session state available"

    state[SELECTED_MODEL_STATE_KEY] = name
    return f"model set to {name} for this session"


@register("workspace")
async def _workspace(args: str, callback_context: Any) -> str:
    from horizon.environment_context import active_environment
    from horizon.tools._paths import path_under_root
    from horizon.workspace_window import WORKSPACE_WINDOW_STATE_KEY, window_dirs

    state = getattr(callback_context, "state", None)
    arg = args.strip()

    if not arg:
        current = window_dirs(state if isinstance(state, dict) else None)
        if current:
            return "workspace focus: " + ", ".join(current)
        return "workspace focus: (whole workspace) — set one with /workspace <subdir>"

    if state is None:
        return "/workspace: no session state available"

    if arg in ("/", "--all"):
        state[WORKSPACE_WINDOW_STATE_KEY] = []
        return "workspace focus cleared — now spanning the whole workspace"

    rel = arg.strip("/")
    root = active_environment().working_dir.resolve()
    resolved, err = path_under_root(rel, root)
    if err is not None or resolved is None or not resolved.is_dir():
        return f"/workspace: directory not found: {arg}"

    state[WORKSPACE_WINDOW_STATE_KEY] = [rel]
    return f"workspace focus set to {rel}/ for this session"


@register("sandbox-upgrade")
async def _sandbox_upgrade(args: str, callback_context: Any) -> str:
    from horizon.conversation.session_start import upgrade_user_sandbox

    invocation_context = getattr(callback_context, "_invocation_context", None)
    user_id = getattr(invocation_context, "user_id", None) or getattr(
        callback_context, "user_id", None
    )
    if not user_id:
        return "/sandbox-upgrade: no user context"

    result = await upgrade_user_sandbox(str(user_id))
    status = result.get("status")
    version = result.get("version", "?")
    if status == "upgraded":
        return f"sandbox upgraded to {version} · /workspace migrated"
    if status == "already_current":
        return f"already on {version} — nothing to upgrade"
    if status == "no_sandbox":
        return "no sandbox to upgrade yet — one provisions on your next session"
    return f"/sandbox-upgrade unavailable: {result.get('reason', 'sandbox backend off')}"


@register("grant")
async def _grant(args: str, callback_context: Any) -> str:
    command = args.strip()
    if not command:
        state = getattr(callback_context, "state", None)
        grants = []
        if state is not None:
            grants = state.get(POLICY_GRANTS_STATE_KEY) or []
        if not grants:
            return "no active grants"
        lines = [f"{len(grants)} active grant(s):"]
        for i, g in enumerate(grants):
            sig = g.get("signature", {})
            lines.append(f"  [{i}] {g.get('tool_name', '?')} {sig}")
        return "\n".join(lines)

    # A shell command reaches the guard chain as bash(command=...),
    # process(action='spawn', command=...), or process(action='write',
    # data=...). grant_matches requires an exact (tool_name, signature)
    # match, so a grant against names.BASH alone leaves the other two
    # shapes blocked; record one grant per shape.
    result = policy_grant(
        action="grant",
        tool_name=names.BASH,
        signature={"command": command},
        tool_context=callback_context,
    )
    if not result.get("granted"):
        return f"/grant failed: {result.get('error', 'unknown')}"
    policy_grant(
        action="grant",
        tool_name=names.PROCESS,
        signature={"command": command},
        tool_context=callback_context,
    )
    policy_grant(
        action="grant",
        tool_name=names.PROCESS,
        signature={"data": command},
        tool_context=callback_context,
    )
    if result.get("duplicate"):
        return f"grant for command={command!r} already active"
    return f"granted command={command!r} for this session"


@register("permissions")
async def _permissions(args: str, callback_context: Any) -> str:
    from horizon.guardrails.permission_rules import (
        load_persisted_rules,
        read_session_grants,
        write_session_grants,
    )

    state = getattr(callback_context, "state", None)
    arg = args.strip().lower()

    if arg == "clear":
        write_session_grants(state, [])
        return "session permission grants cleared"

    grants = read_session_grants(state)

    from horizon.environment_context import active_environment

    try:
        persisted = await load_persisted_rules(active_environment())
    except RuntimeError:
        persisted = []

    lines: list[str] = []
    if grants:
        lines.append(f"{len(grants)} session grant(s):")
        for g in grants:
            target = g.get("commandPrefix") or g.get("toolName")
            lines.append(f"  · {g.get('toolName')} {target}")
    else:
        lines.append("no session permission grants")
    lines.append(
        f"{len(persisted)} persisted rule(s) in .lha/permissions.jsonl"
    )
    lines.append("use `/permissions clear` to drop session grants")
    return "\n".join(lines)


@register("routines")
async def _routines(args: str, callback_context: Any) -> str:
    from horizon.auth.identity import get_user_id_from_context
    from horizon.scheduler.routine_store import get_routine_store

    user_id = get_user_id_from_context() or ""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    store = get_routine_store()

    if sub == "remove" and len(parts) > 1:
        ok = await store.cancel(parts[1].strip(), user_id)
        return (
            f"Removed routine '{parts[1].strip()}'."
            if ok
            else "No such routine."
        )

    rows = await store.list_for_user(user_id)
    if not rows:
        return "No routines scheduled."
    lines = [
        f"- {r.id}  ({r.schedule})  next: {r.next_fire_at.isoformat()}"
        for r in rows
    ]
    return "Scheduled routines:\n" + "\n".join(lines)


@register("yolo")
async def _yolo(args: str, callback_context: Any) -> str:
    state = getattr(callback_context, "state", None)
    if state is None:
        return "/yolo: no session state available"

    current = state.get(APPROVAL_MODE_STATE_KEY, "default")
    new_mode = "yolo" if current != "yolo" else "default"
    state[APPROVAL_MODE_STATE_KEY] = new_mode

    if new_mode == "yolo":
        return (
            "Approval mode: YOLO — risky commands auto-approved this session. "
            "Catastrophic and secret-exfil blocks still apply."
        )
    return "Approval mode: default — interactive approval required for risky commands."


__all__ = [
    "BUILTIN_COMMAND_REGISTRY",
    "register",
    "reload",
]
