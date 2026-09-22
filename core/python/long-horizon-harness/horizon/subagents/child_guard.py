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

"""Child-scoped ``before_tool_callback`` for delegated sub-agents.

Layers restrictions on top of the global ``policies_guard``:

* A deny-by-default profile allowlist — when a ``ChildProfile`` with a
  non-None ``allowed_tool_names`` is active, only those tools may run.
* Inherited parent grants — the child sees exactly the policy exemptions
  the parent earned, and no more. Children never write new grants: granting
  is `/grant`, a user-only slash command, not a tool any agent (parent or
  child) can call.

It also runs the same ``exfil_guard`` → ``policies_guard`` chain the root
agent runs (see ``horizon/agent.py``).

**Resurfacing mode.** When a blocking ``delegate`` drives the child it sets a
bubble budget (``resurface_context``). In that mode an ``ask_user`` verdict is
surfaced to the live user via ``request_confirmation`` (the child runner pauses;
``delegate`` re-raises it on the parent turn) instead of being denied. On
approval the op runs and a session grant is written so the child's later
same-shape ops auto-allow. With no budget left (post-approval resume pass) or no
budget at all (background ``agent`` / routine = headless), the existing
``ask_is_deny`` behavior holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from horizon.environment_context import active_environment
from horizon.guardrails.exfil_guard import exfil_guard
from horizon.guardrails.permission_guard import resolve_permission_decision
from horizon.guardrails.permission_rules import (
    read_session_grants,
    write_session_grants,
)
from horizon.guardrails.policies import policies_guard
from horizon.guardrails.policy_grants import POLICY_GRANTS_STATE_KEY
from horizon.subagents.ask_parent import ASK_PARENT_TOOL_NAME
from horizon.subagents.profiles import ChildProfile
from horizon.subagents.resurface_context import (
    bubble_budget,
    try_consume_bubble,
)
from horizon.tools._hitl import request_user_confirmation

ChildGuard = Callable[..., Awaitable[dict[str, Any] | None]]


def _deny_msg(tool_name: str, profile: ChildProfile) -> str:
    allowed = sorted(profile.allowed_tool_names or [])
    return (
        f"tool {tool_name!r} is not permitted under the {profile.name!r} "
        f"profile (allowed: {allowed})."
    )


def _normalize_grants(parent_grants: Any) -> list[dict[str, Any]]:
    if not isinstance(parent_grants, list):
        return []
    return [g for g in parent_grants if isinstance(g, dict)]


def _active_env() -> Any | None:
    try:
        return active_environment()
    except RuntimeError:
        return None


def _write_grant(
    state: Any, tool_name: str, proposed_prefix: list[str] | None
) -> None:
    if state is None:
        return
    rule: dict[str, Any] = {"toolName": tool_name, "decision": "allow"}
    if proposed_prefix:
        rule["commandPrefix"] = proposed_prefix
    write_session_grants(state, [*read_session_grants(state), rule])


async def _resurface_decision(
    *, tool_name: str, args: Any, tool_context: Any, state: Any
) -> dict[str, Any] | None:
    """Permission-aware decision for a child running under a live parent turn."""
    agent_name = getattr(tool_context, "agent_name", None)
    (
        decision,
        deny_rule,
        proposed_prefix,
        command,
    ) = await resolve_permission_decision(
        _active_env(),
        tool_name=tool_name,
        args=args,
        state=state,
        agent_name=agent_name,
    )
    if decision == "allow":
        return None
    if decision == "deny":
        src = f" [{deny_rule.source}]" if deny_rule and deny_rule.source else ""
        msg = (
            deny_rule.deny_message
            if deny_rule and deny_rule.deny_message
            else f"tool call denied by permission rule{src}: {tool_name}"
        )
        return {"error": msg, "permission_denied": True}

    # decision == "ask_user"
    tc = getattr(tool_context, "tool_confirmation", None)
    if tc is not None:
        if getattr(tc, "confirmed", False):
            _write_grant(state, tool_name, proposed_prefix)
            return None
        return {
            "error": f"User declined the {tool_name} call.",
            "permission_denied": True,
        }

    if try_consume_bubble():
        summary = (
            f"Run: {command}"
            if command is not None
            else f"Allow tool: {tool_name}"
        )
        label_target = (
            ", ".join(f"`{p}`" for p in proposed_prefix)
            if proposed_prefix
            else tool_name
        )
        payload = {
            "kind": "permission",
            "toolName": tool_name,
            "summary": summary,
            "question": summary,
            "choices": [
                "Yes, once",
                f"Yes — allow {label_target} this session",
                f"Always allow {label_target}",
                "Decline",
            ],
        }
        request_user_confirmation(tool_context, hint=summary, payload=payload)
        return {
            "error": (
                f"This {tool_name} call requires user approval. Awaiting confirmation."
            ),
            "confirmation_required": True,
        }

    return {
        "error": (
            f"This delegated task needs approval for {tool_name}, but the single "
            "approval available per delegate run was already used. Split the work "
            "into separate delegate calls or pre-grant the operation."
        ),
        "permission_denied": True,
        "resurface_exhausted": True,
    }


def make_child_policy_guard(
    *,
    parent_grants: Any,
    profile: ChildProfile | None,
) -> ChildGuard:
    grants = _normalize_grants(parent_grants)

    async def guard(
        *, tool: Any, args: Any, tool_context: Any
    ) -> dict[str, Any] | None:
        tool_name = getattr(tool, "name", "") or ""

        # ask_parent is the child's own escalation channel — it manages its own
        # HITL + bubble budget, so never gate it behind the profile allowlist or
        # the permission ask-layer (which would prompt to approve the escalation
        # itself). It takes only a question string, so there is no exfil vector.
        if tool_name == ASK_PARENT_TOOL_NAME:
            return None

        if (
            profile is not None
            and profile.allowed_tool_names is not None
            and tool_name not in profile.allowed_tool_names
        ):
            return {
                "error": _deny_msg(tool_name, profile),
                "denied_by_profile": True,
            }

        state = getattr(tool_context, "state", None)
        if (
            grants
            and state is not None
            and not state.get(POLICY_GRANTS_STATE_KEY)
        ):
            try:
                state[POLICY_GRANTS_STATE_KEY] = list(grants)
            except (AttributeError, TypeError):
                pass

        exfil = await exfil_guard(
            tool=tool, args=args, tool_context=tool_context
        )
        if exfil is not None:
            return exfil

        if bubble_budget() is None:
            # Headless: background `agent` / routine run — no live user to ask, so
            # an ask-classified op stays fail-closed (ask_is_deny=True).
            return await policies_guard(
                tool=tool,
                args=args,
                tool_context=tool_context,
                ask_is_deny=True,
            )

        # Resurfacing: destructive ops still hard-deny; ask-classified ops fall
        # through to the permission resolution, which surfaces to the user.
        pol = await policies_guard(
            tool=tool, args=args, tool_context=tool_context, ask_is_deny=False
        )
        if pol is not None:
            return pol
        return await _resurface_decision(
            tool_name=tool_name,
            args=args,
            tool_context=tool_context,
            state=state,
        )

    return guard


__all__ = ["make_child_policy_guard"]
