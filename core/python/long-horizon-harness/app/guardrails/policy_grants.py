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

"""Per-session policy grants — HITL bypass for the policies guard.

When ``policies_guard`` blocks a tool call, the model can (only after
explicit user approval in the conversation) call
``policy_grant(action='grant', tool_name=..., signature=...)`` to record
a per-session exemption. ``policies_guard`` consults active grants
BEFORE evaluating rules; a matching grant short-circuits the block.

Grants live in ``session.state["_policy_grants"]`` so they:
  * Travel with the session — durable across plugin restarts via the
    same session-state mechanism as the iteration budget.
  * Are garbage-collected when the session ends — no cross-session leak.
  * Stay scoped to the user who granted them.

A grant matches a call iff:
  * grant.tool_name == call.tool_name, AND
  * every (k, v) in grant.signature is present verbatim in call.args.

The signature-subset model keeps grants narrow: a grant for
``{"command": "rm -rf build/"}`` does NOT allow ``"rm -rf /"``.
"""

from __future__ import annotations

from typing import Any, Literal

POLICY_GRANTS_STATE_KEY = "_policy_grants"


def _state(tool_context: Any) -> Any:
    state = getattr(tool_context, "state", None)
    if state is None:
        raise ValueError("tool_context.state is required")
    return state


def _active_grants(state: dict[str, Any]) -> list[dict[str, Any]]:
    grants = state.get(POLICY_GRANTS_STATE_KEY)
    if not isinstance(grants, list):
        return []
    return [g for g in grants if isinstance(g, dict)]


def grant_matches(grant: dict[str, Any], tool_name: str, args: Any) -> bool:
    """True if `grant` exempts the (tool_name, args) call."""
    if grant.get("tool_name") != tool_name:
        return False
    signature = grant.get("signature")
    if not isinstance(signature, dict) or not signature:
        return False
    if not isinstance(args, dict):
        return False
    for key, expected in signature.items():
        if args.get(key) != expected:
            return False
    return True


def find_matching_grant(
    state: dict[str, Any], tool_name: str, args: Any
) -> dict[str, Any] | None:
    for grant in _active_grants(state):
        if grant_matches(grant, tool_name, args):
            return grant
    return None


def grant_matches_loose(
    grant: dict[str, Any], tool_name: str, args: Any
) -> bool:
    """Like :func:`grant_matches` but a string signature value matches when it
    is a *substring* of the call's arg, not only on exact equality.

    Lets an approved command clear the block even when the model re-wraps it
    (``cd … && curl … && echo``). Used ONLY by the exfil guard's path/host
    tiers — NOT by the destructive-op policy guard, where exactness is the
    whole point (a grant for ``rm -rf build/`` must not clear ``rm -rf /``).
    """
    if grant.get("tool_name") != tool_name:
        return False
    signature = grant.get("signature")
    if not isinstance(signature, dict) or not signature:
        return False
    if not isinstance(args, dict):
        return False
    for key, expected in signature.items():
        value = args.get(key)
        if value == expected:
            continue
        if (
            isinstance(value, str)
            and isinstance(expected, str)
            and expected
            and (expected in value)
        ):
            continue
        return False
    return True


def find_matching_grant_loose(
    state: dict[str, Any], tool_name: str, args: Any
) -> dict[str, Any] | None:
    for grant in _active_grants(state):
        if grant_matches_loose(grant, tool_name, args):
            return grant
    return None


def _grant(
    tool_name: str,
    signature: dict[str, str],
    tool_context: Any,
) -> dict[str, Any]:
    if not tool_name:
        return {"granted": False, "error": "tool_name is required"}
    if not isinstance(signature, dict) or not signature:
        return {
            "granted": False,
            "error": "signature must be a non-empty dict of arg_name → value",
        }
    for key, value in signature.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return {
                "granted": False,
                "error": "signature keys and values must be strings",
            }

    state = _state(tool_context)
    grants = state.get(POLICY_GRANTS_STATE_KEY)
    if not isinstance(grants, list):
        grants = []

    new_grant = {"tool_name": tool_name, "signature": dict(signature)}
    for existing in grants:
        if (
            existing.get("tool_name") == tool_name
            and existing.get("signature") == new_grant["signature"]
        ):
            return {"granted": True, "grant": existing, "duplicate": True}

    grants.append(new_grant)
    state[POLICY_GRANTS_STATE_KEY] = grants
    return {"granted": True, "grant": new_grant}


def _list(tool_context: Any) -> dict[str, Any]:
    state = _state(tool_context)
    grants = _active_grants(state)
    return {"count": len(grants), "grants": list(grants)}


def _revoke(index: int, tool_context: Any) -> dict[str, Any]:
    state = _state(tool_context)
    grants = state.get(POLICY_GRANTS_STATE_KEY)
    if not isinstance(grants, list) or index < 0 or index >= len(grants):
        return {"revoked": False, "error": "index out of range"}
    removed = grants.pop(index)
    state[POLICY_GRANTS_STATE_KEY] = grants
    return {"revoked": True, "grant": removed}


def policy_grant(
    action: Literal["grant", "list", "revoke"],
    *,
    tool_name: str | None = None,
    signature: dict[str, str] | None = None,
    index: int | None = None,
    tool_context: Any,
) -> dict[str, Any]:
    """Manage per-session HITL bypass grants for ``policies_guard``.
    ``action`` picks the operation:

    - ``grant``: record a per-session exemption. Requires ``tool_name``
      (the blocked tool, e.g. ``"bash"``) and ``signature`` (a
      non-empty ``{arg_name: value}`` dict that must match the call
      verbatim). ONLY call after the user has explicitly approved the
      blocked action in the conversation.
    - ``list``: return the active per-session grants.
    - ``revoke``: drop a grant by its index in the list returned by
      ``action='list'``. Requires ``index``.
    """
    if action == "grant":
        if tool_name is None:
            return {
                "granted": False,
                "error": "action='grant' requires 'tool_name'",
            }
        if signature is None:
            return {
                "granted": False,
                "error": "action='grant' requires 'signature'",
            }
        return _grant(tool_name, signature, tool_context)
    if action == "list":
        return _list(tool_context)
    if action == "revoke":
        if index is None:
            return {
                "revoked": False,
                "error": "action='revoke' requires 'index'",
            }
        return _revoke(index, tool_context)
    return {"error": f"Unknown action {action!r}. Use: grant, list, revoke."}


__all__ = [
    "POLICY_GRANTS_STATE_KEY",
    "find_matching_grant",
    "find_matching_grant_loose",
    "grant_matches",
    "grant_matches_loose",
    "policy_grant",
]
