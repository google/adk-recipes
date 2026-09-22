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

"""Central permission gate (before_tool_callback).

Read-only and self-confirming tools pass through. Everything else resolves a
decision from the layered rules (defaults → .lha/permissions.jsonl → session
grants) and, when the decision is ask_user, dispatches ADK's
request_confirmation and pauses; on resume it records the grant per the chosen
outcome (gemini ToolConfirmationOutcome vocabulary).
"""

from __future__ import annotations

import contextvars
from typing import Any

from horizon.environment_context import active_environment
from horizon.guardrails.command_classify import (
    command_prefix,
    has_command_substitution,
    split_segments,
    strip_wrapper,
)
from horizon.guardrails.command_safety import classify as classify_command
from horizon.guardrails.permission_rules import (
    PermissionRule,
    append_persisted_rule,
    effective_rules,
    read_approval_mode,
    read_session_grants,
    resolve_decision,
    write_session_grants,
)
from horizon.tools import names
from horizon.tools._hitl import request_user_confirmation

_HEADLESS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "permission_headless", default=False
)


def set_headless_mode(value: bool) -> contextvars.Token[bool]:
    """Enable/disable headless mode for the current context.

    In headless mode there is no user to approve, so an ``ask_user`` decision on a
    **shell** command (bash / process write) is allowed — it runs in the
    routine's isolated sandbox — while a non-shell ``ask_user`` becomes a deny.
    Set by the routine fire path; returns a token for reset.
    """
    return _HEADLESS.set(bool(value))


def reset_headless_mode(token: contextvars.Token[bool]) -> None:
    _HEADLESS.reset(token)


READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        names.READ,
        names.SEARCH_FILES,
        names.PRELOAD_MEMORY,
        # Skill loading (a skill's instructions, or a bundled reference/
        # asset/script file via resource=) injects text into context, no
        # side effects. action='reload' is also read-only from the model's
        # perspective (it only refreshes the catalog it already sees).
        names.LOAD_SKILL,
        # Read-only research: fetches web info, mutates nothing the user owns.
        # Outbound safety still applies — exfil_guard runs before this gate.
        names.WEB_RESEARCH,
    }
)
SELF_CONFIRMING_TOOLS: frozenset[str] = frozenset({names.CLARIFY})


def _is_memory_search(tool_name: str, args: Any) -> bool:
    """memory(action='search') is the former session_search tool, and was
    read-only (bypassed permission resolution unconditionally). memory's
    default action='add' is a write and must NOT get that bypass — it stays
    on the _BENIGN_SIDE_EFFECT_TOOLS default-allow rule, which an overlay or
    session grant can still override. Deliberately action-aware rather than
    inheriting READ_ONLY_TOOLS membership wholesale for the merged tool."""
    return (
        tool_name == names.MEMORY
        and isinstance(args, dict)
        and args.get("action") == "search"
    )


# Spawning a sub-agent is not itself a privileged op: the child runs its own
# guard chain (child_guard.py) — exfil hard-block and risky ops are gated
# per-op (a blocking call resurfaces them to the user; a background one is
# headless so they hard-deny and surface in the child's reported result). The
# coarse spawn-time prompt grants the child nothing, so it's pure friction.
SUBAGENT_TOOLS: frozenset[str] = frozenset({names.SUBAGENT})


def _active_env() -> Any | None:
    try:
        return active_environment()
    except RuntimeError:
        return None


def _shell_command(tool_name: str, args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    if tool_name == names.BASH:
        cmd = args.get("command")
        return cmd if isinstance(cmd, str) else None
    if tool_name == names.PROCESS and args.get("action") == "write":
        data = args.get("data")
        return data if isinstance(data, str) else None
    # process(action='spawn') is a NEW shell execution path (bash's former
    # background=True moved here) — it must run through the exact same
    # command_safety classification and command-substitution demotion as
    # every other shell entry point, or one argument bypasses the whole
    # anti-obfuscation layer.
    if tool_name == names.PROCESS and args.get("action") == "spawn":
        command = args.get("command")
        return command if isinstance(command, str) else None
    return None


def _shell_decision(
    rules: list[PermissionRule],
    tool_name: str,
    command: str,
    agent_name: str | None,
) -> tuple[str, PermissionRule | None, list[str]]:
    """Per-segment: deny if any segment denies; ask with the unsafe-segment
    prefixes if any segment is not allowed; otherwise allow."""
    segments = split_segments(strip_wrapper(command)) or [command.strip()]
    deny_rule: PermissionRule | None = None
    ask_prefixes: list[str] = []
    for seg in segments:
        decision, rule = resolve_decision(
            rules,
            tool_name=tool_name,
            args={"command": seg},
            command=seg,
            agent_name=agent_name,
        )
        if decision == "allow" and has_command_substitution(seg):
            decision = "ask_user"
            rule = None
        # command_safety "ask" demotes a default allow — but an explicit grant or
        # overlay allow (user-approved) overrides it, so "approve for this session
        # / always" sticks. The catastrophic floor (command_safety deny + policy
        # seed) is enforced earlier in policies_guard (Layer C).
        if decision == "allow" and (
            rule is None or rule.source not in {"grant", "overlay"}
        ):
            verdict = classify_command(seg)
            if verdict is not None and verdict[0] == "ask":
                decision = "ask_user"
                rule = None
        if decision == "deny":
            deny_rule = rule
        elif decision != "allow":
            ask_prefixes.append(command_prefix(seg))
    if deny_rule is not None:
        return ("deny", deny_rule, [])
    if ask_prefixes:
        return ("ask_user", None, list(dict.fromkeys(ask_prefixes)))
    return ("allow", None, [])


async def resolve_permission_decision(
    env: Any,
    *,
    tool_name: str,
    args: Any,
    state: Any,
    agent_name: str | None,
) -> tuple[str, PermissionRule | None, list[str] | None, str | None]:
    """Resolve allow / deny / ask_user for a tool call from the layered rules.

    Shared by the parent gate and the child (delegate) guard so shell-segment
    handling and rule precedence never drift. Returns ``(decision, deny_rule,
    proposed_prefix, command)``: the matched deny rule only when denying, the
    unsafe-segment prefixes to propose on an ask (shell only), and the extracted
    shell command (or None for non-shell tools)."""
    command = _shell_command(tool_name, args)
    rules = await effective_rules(env, read_session_grants(state))
    if command is not None:
        decision, deny_rule, ask_prefixes = _shell_decision(
            rules, tool_name, command, agent_name
        )
        return decision, deny_rule, (ask_prefixes or None), command
    decision, matched = resolve_decision(
        rules,
        tool_name=tool_name,
        args=args,
        command=None,
        agent_name=agent_name,
    )
    return decision, (matched if decision == "deny" else None), None, command


def is_headless() -> bool:
    """True when running with no user to prompt (routine fire path)."""
    return _HEADLESS.get()


def _resolve_outcome(
    payload: Any, ordered_outcomes: list[tuple[str, str]]
) -> str:
    """Map a resumed confirmation to an outcome key via ``choice`` (label/index, both clients) or legacy ``outcome``; unparseable → session grant."""
    keys = [k for k, _ in ordered_outcomes]
    labels = [lab for _, lab in ordered_outcomes]
    if isinstance(payload, dict):
        explicit = payload.get("outcome")
        if explicit in keys:
            return str(explicit)
        choice = payload.get("choice") or payload.get("answer")
        if isinstance(choice, str):
            choice = choice.strip()
            if choice in labels:
                return keys[labels.index(choice)]
            if choice.isdigit() and 0 <= int(choice) < len(keys):
                return keys[int(choice)]
    return "proceed_always"


async def permission_guard(
    *, tool: Any, args: Any, tool_context: Any
) -> dict[str, Any] | None:
    tool_name = getattr(tool, "name", "") or ""
    if (
        not tool_name
        or tool_name in READ_ONLY_TOOLS
        or _is_memory_search(tool_name, args)
        or tool_name in SELF_CONFIRMING_TOOLS
        or tool_name in SUBAGENT_TOOLS
    ):
        return None

    state = getattr(tool_context, "state", None)
    agent_name = getattr(tool_context, "agent_name", None)
    env = _active_env()

    (
        decision,
        deny_rule,
        proposed_prefix,
        command,
    ) = await resolve_permission_decision(
        env,
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
    if read_approval_mode(state) == "yolo" and not _HEADLESS.get():
        return None

    if _HEADLESS.get():
        # Headless == a routine run. A shell command that would otherwise prompt
        # is allowed: it executes in the routine's own isolated `lhart-` sandbox
        # (the blast radius), and exfil_guard / policies_guard already ran
        # ahead of this gate and still block secret-exfil and destructive ops. A non-shell
        # approval has no such sandbox bound, so it stays fail-closed.
        if command is not None:
            return None
        return {
            "error": (
                f"This {tool_name} call needs approval, but no user is present "
                "(headless routine run). Auto-denied — design the task to avoid "
                "operations that require approval."
            ),
            "permission_denied": True,
            "headless_denied": True,
        }
    proposed_rule: dict[str, Any] = {"toolName": tool_name, "decision": "allow"}
    label_target = tool_name
    if proposed_prefix is not None:
        proposed_rule["commandPrefix"] = proposed_prefix
        label_target = ", ".join(f"`{p}`" for p in proposed_prefix)

    summary = (
        f"Run: {command}" if command is not None else f"Allow tool: {tool_name}"
    )
    # Ordered, cancel last: choices[i] maps to ordered_outcomes[i] on resume, and
    # both GE and the web card treat the trailing choice as Decline.
    ordered_outcomes = [
        ("proceed_once", "Yes, once"),
        ("proceed_always", f"Yes — allow {label_target} this session"),
        ("proceed_always_and_save", f"Always allow {label_target}"),
        ("cancel", "Decline"),
    ]
    payload = {
        "kind": "permission",  # styling hint for the web card
        "toolName": tool_name,
        "summary": summary,
        "proposedRule": proposed_rule,
        "question": summary,
        "choices": [label for _, label in ordered_outcomes],
    }

    tc = request_user_confirmation(tool_context, hint=summary, payload=payload)
    if tc is None:
        return {
            "error": (
                f"This {tool_name} call requires user approval. Awaiting confirmation."
            ),
            "confirmation_required": True,
        }
    if not getattr(tc, "confirmed", False):
        return {
            "error": f"User declined the {tool_name} call.",
            "permission_denied": True,
        }

    outcome = _resolve_outcome(getattr(tc, "payload", None), ordered_outcomes)
    if outcome == "cancel":
        return {
            "error": f"User declined the {tool_name} call.",
            "permission_denied": True,
        }
    if outcome == "proceed_always":
        write_session_grants(
            state, [*read_session_grants(state), proposed_rule]
        )
    elif outcome == "proceed_always_and_save" and env is not None:
        await append_persisted_rule(env, proposed_rule)
    return None


__all__ = [
    "READ_ONLY_TOOLS",
    "SELF_CONFIRMING_TOOLS",
    "SUBAGENT_TOOLS",
    "is_headless",
    "permission_guard",
    "reset_headless_mode",
    "resolve_permission_decision",
    "set_headless_mode",
]
