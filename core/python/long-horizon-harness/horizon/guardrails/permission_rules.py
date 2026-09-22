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

"""Permission rules: gemini-cli rule schema in this repo's .lha/*.jsonl form.

Layers (low → high precedence): built-in defaults → .lha/permissions.jsonl →
session grants. Resolution is deny-wins, else last-match-wins.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from horizon.guardrails._jsonl import iter_jsonl_objects
from horizon.guardrails._overlay import (
    is_local_env,
    overlay_cache_key,
    read_overlay_text,
)
from horizon.guardrails._regex_safety import safe_regex
from horizon.guardrails.command_classify import significant_tokens
from horizon.tools import names
from horizon.tools.names import apply_tool_aliases

_logger = logging.getLogger(__name__)

PERMISSION_OVERLAY_FILENAME = ".lha/permissions.jsonl"
PERMISSION_GRANTS_STATE_KEY = "permission_grants"
APPROVAL_MODE_STATE_KEY = "approval_mode"
_VALID_DECISIONS = frozenset({"allow", "deny", "ask_user"})


@dataclass(frozen=True)
class PermissionRule:
    tool_names: tuple[str, ...]
    command_prefixes: tuple[str, ...]
    command_regex: str | None
    args_pattern: str | None
    decision: str
    deny_message: str | None
    subagent: str | None
    source: str | None = None


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def _as_aliased_tuple(value: Any) -> tuple[str, ...]:
    # A rule's toolName can be a since-renamed legacy name (a persisted
    # .lha/permissions.jsonl file, or a session grant recorded before a
    # rename). Aliasing here, ahead of every downstream match/narrowing
    # check, is what keeps a rule pointed at the live tool.
    return tuple(apply_tool_aliases(v) for v in _as_tuple(value))


def parse_rule(obj: Any, *, trusted: bool = False) -> PermissionRule | None:
    """Normalize one gemini-shaped rule dict → PermissionRule, or None if invalid."""
    if not isinstance(obj, dict):
        return None
    decision = obj.get("decision")
    if decision not in _VALID_DECISIONS:
        return None
    tool_names = _as_aliased_tuple(obj.get("toolName"))
    if not tool_names:
        return None
    command_regex = (
        obj.get("commandRegex")
        if isinstance(obj.get("commandRegex"), str)
        else None
    )
    if (
        command_regex is not None
        and not trusted
        and not safe_regex(command_regex)
    ):
        _logger.warning("permissions: dropping rule with unsafe commandRegex")
        return None
    args_pattern = (
        obj.get("argsPattern")
        if isinstance(obj.get("argsPattern"), str)
        else None
    )
    if (
        args_pattern is not None
        and not trusted
        and not safe_regex(args_pattern)
    ):
        _logger.warning("permissions: dropping rule with unsafe argsPattern")
        return None
    return PermissionRule(
        tool_names=tool_names,
        command_prefixes=_as_tuple(obj.get("commandPrefix")),
        command_regex=command_regex,
        args_pattern=args_pattern,
        decision=decision,
        deny_message=obj.get("denyMessage")
        if isinstance(obj.get("denyMessage"), str)
        else None,
        subagent=obj.get("subagent")
        if isinstance(obj.get("subagent"), str)
        else None,
        source=obj.get("source")
        if isinstance(obj.get("source"), str)
        else None,
    )


def _tool_matches(rule: PermissionRule, tool_name: str) -> bool:
    return "*" in rule.tool_names or tool_name in rule.tool_names


def _prefix_matches(command: str, prefix: str) -> bool:
    if command == prefix or command.startswith(prefix + " "):
        return True
    # Flag-tolerant fallback so an approved `bq query` also covers
    # `bq --project_id=X query …`. Only for a flagless rule prefix: one that names
    # a flag (`git push --force`) must keep matching literally, or dropping flags
    # would widen it onto the plain command it was written to single out.
    prefix_tokens = prefix.split()
    if not prefix_tokens or any(t.startswith("-") for t in prefix_tokens):
        return False
    return significant_tokens(command)[: len(prefix_tokens)] == prefix_tokens


def rule_matches(
    rule: PermissionRule,
    *,
    tool_name: str,
    args: Any,
    command: str | None,
    agent_name: str | None,
) -> bool:
    if not _tool_matches(rule, tool_name):
        return False
    if rule.subagent is not None and rule.subagent != agent_name:
        return False
    if rule.command_prefixes:
        if command is None or not any(
            _prefix_matches(command, p) for p in rule.command_prefixes
        ):
            return False
    if rule.command_regex is not None:
        if command is None:
            return False
        try:
            if not re.search(rule.command_regex, command):
                return False
        except re.error:
            return False
    if rule.args_pattern is not None:
        blob = (
            json.dumps(args, sort_keys=True, default=str)
            if isinstance(args, dict)
            else ""
        )
        try:
            if not re.search(rule.args_pattern, blob):
                return False
        except re.error:
            return False
    return True


def resolve_decision(
    rules: list[PermissionRule],
    *,
    tool_name: str,
    args: Any,
    command: str | None,
    agent_name: str | None = None,
) -> tuple[str, PermissionRule | None]:
    """Deny-wins, else last-match-wins. Defaults to ('ask_user', None)."""
    matches = [
        r
        for r in rules
        if rule_matches(
            r,
            tool_name=tool_name,
            args=args,
            command=command,
            agent_name=agent_name,
        )
    ]
    deny = next((r for r in reversed(matches) if r.decision == "deny"), None)
    if deny is not None:
        return ("deny", deny)
    if matches:
        return (matches[-1].decision, matches[-1])
    return ("ask_user", None)


def _parse_jsonl(text: str) -> list[PermissionRule]:
    out: list[PermissionRule] = []
    for obj in iter_jsonl_objects(text, context="permissions"):
        rule = parse_rule(obj)
        if rule is not None:
            out.append(rule)
    return out


# Local-backend overlay cache (mtime hot-reload). The sandbox backend can't stat
# over the env interface, so it uses ``_overlay_cache`` keyed by sandbox identity
# (invalidated when a rule is appended); each backend instance lazily reloads the
# durable in-sandbox file.
_cache: dict[str, Any] = {"path": None, "mtime": None, "rules": None}
_overlay_cache: dict[str, list[PermissionRule]] = {}


def _load_local_rules(working_dir: Path) -> list[PermissionRule]:
    """Read .lha/permissions.jsonl from the local fs (hot-reloaded by mtime)."""
    path = working_dir / PERMISSION_OVERLAY_FILENAME
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    cache_key = str(path)
    if (
        _cache["path"] == cache_key
        and _cache["mtime"] == mtime
        and _cache["rules"] is not None
        and mtime is not None
    ):
        return list(_cache["rules"])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    rules = _parse_jsonl(text)
    _cache.update(path=cache_key, mtime=mtime, rules=rules)
    return list(rules)


async def _load_interface_rules(
    env: Any, working_dir: Path
) -> list[PermissionRule]:
    """Read the overlay from inside the sandbox via the env interface (the file lives
    in the sandbox, not on the backend host), cached per sandbox identity."""
    key = overlay_cache_key(env)
    cached = _overlay_cache.get(key)
    if cached is not None:
        return list(cached)
    text = await read_overlay_text(
        env, working_dir / PERMISSION_OVERLAY_FILENAME
    )
    rules = _parse_jsonl(text)
    _overlay_cache[key] = rules
    return list(rules)


async def load_persisted_rules(env: Any) -> list[PermissionRule]:
    """Read .lha/permissions.jsonl for ``env``. Local backend reads the host fs;
    the sandbox backend reads through the env interface so the durable in-sandbox
    overlay (not an ephemeral container path) is what takes effect."""
    if env is None:
        return []
    working_dir = getattr(env, "working_dir", None)
    if working_dir is None:
        return []
    if is_local_env(env):
        return _load_local_rules(working_dir)
    return await _load_interface_rules(env, working_dir)


async def append_persisted_rule(env: Any, rule: dict[str, Any]) -> None:
    """Append one gemini-shaped rule as a JSONL line to .lha/permissions.jsonl,
    through the same backend the reader uses so the write is durable and visible."""
    working_dir = getattr(env, "working_dir", None)
    if working_dir is None:
        return
    path = working_dir / PERMISSION_OVERLAY_FILENAME
    line = json.dumps(rule) + "\n"
    if is_local_env(env):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        _cache["mtime"] = None  # invalidate
        return
    # Read-before-write: only "no file yet" is empty; let any other read error
    # propagate so we never overwrite an existing overlay we failed to read.
    try:
        raw = await env.read_file(path)
        current = (
            raw.decode("utf-8", errors="replace")
            if isinstance(raw, (bytes, bytearray))
            else str(raw)
        )
    except FileNotFoundError:
        current = ""
    await env.write_file(path, current + line)
    _overlay_cache.pop(overlay_cache_key(env), None)  # next load re-reads


_SANDBOX_WRITE_TOOLS: tuple[str, ...] = (
    names.WRITE,
    names.EDIT,
    names.ARTIFACT,
)
# Benign non-shell tools that mutate only the user's own data, opened to cut
# everyday friction. `routine` (unattended runs) deliberately stays on the
# `*: ask_user` fallback. `load_skill` needs no entry: it's in
# permission_guard.READ_ONLY_TOOLS and bypasses rule resolution entirely.
_BENIGN_SIDE_EFFECT_TOOLS: tuple[str, ...] = (names.MEMORY,)

_DEFAULT_RULE_DICTS: tuple[dict[str, Any], ...] = (
    {"toolName": "*", "decision": "ask_user"},
    *({"toolName": t, "decision": "allow"} for t in _SANDBOX_WRITE_TOOLS),
    *({"toolName": t, "decision": "allow"} for t in _BENIGN_SIDE_EFFECT_TOOLS),
    # Shell tools run by default; the scary-op gating lives in _shell_decision
    # (command_safety + command-substitution), not in this seed.
    {"toolName": names.BASH, "decision": "allow"},
    {"toolName": names.PROCESS, "decision": "allow"},
)

DEFAULT_RULES: tuple[PermissionRule, ...] = tuple(
    r
    for r in (parse_rule(d, trusted=True) for d in _DEFAULT_RULE_DICTS)
    if r is not None
)

_TOOLS_REQUIRING_NARROWING: frozenset[str] = frozenset(
    {names.BASH, names.PROCESS}
)


def _is_blanket_allow(rule: PermissionRule) -> bool:
    return (
        rule.decision == "allow"
        and any(t in _TOOLS_REQUIRING_NARROWING for t in rule.tool_names)
        and not rule.command_prefixes
        and rule.command_regex is None
        and rule.args_pattern is None
    )


def _stamp(rules: list[PermissionRule], source: str) -> list[PermissionRule]:
    out = []
    for r in rules:
        if _is_blanket_allow(r):
            _logger.warning(
                "permissions: dropping blanket allow for %s from %s",
                r.tool_names,
                source,
            )
            continue
        out.append(replace(r, source=source))
    return out


async def effective_rules(
    env: Any, session_grants: list[dict[str, Any]] | None
) -> list[PermissionRule]:
    """Layered rules low→high: defaults → persisted (.lha overlay) → session grants."""
    rules = [replace(r, source="default") for r in DEFAULT_RULES]
    rules += _stamp(await load_persisted_rules(env), "overlay")
    grants = [parse_rule(g) for g in (session_grants or [])]
    rules += _stamp([g for g in grants if g is not None], "grant")
    return rules


def read_session_grants(state: Any) -> list[dict[str, Any]]:
    """Session permission grants from an ADK ``State`` OR a plain dict. ``State``
    is NOT a dict subclass, so this duck-types ``.get`` rather than isinstance."""
    if state is None:
        return []
    getter = getattr(state, "get", None)
    if not callable(getter):
        return []
    grants = getter(PERMISSION_GRANTS_STATE_KEY)
    return list(grants) if isinstance(grants, list) else []


def write_session_grants(state: Any, grants: list[dict[str, Any]]) -> bool:
    """Persist session grants by reassigning the key — ADK ``State`` records the
    delta on ``__setitem__``; an in-place list mutation would not. Returns False
    if ``state`` can't hold it."""
    if state is None:
        return False
    try:
        state[PERMISSION_GRANTS_STATE_KEY] = list(grants)
    except (TypeError, KeyError, AttributeError):
        return False
    return True


def read_approval_mode(state: Any) -> str:
    """Read approval_mode from session state. Returns "default" unless state
    holds "yolo". Duck-types .get like read_session_grants."""
    if state is None:
        return "default"
    getter = getattr(state, "get", None)
    if not callable(getter):
        return "default"
    mode = getter(APPROVAL_MODE_STATE_KEY)
    return mode if mode in {"default", "yolo"} else "default"


__all__ = [
    "APPROVAL_MODE_STATE_KEY",
    "DEFAULT_RULES",
    "PERMISSION_GRANTS_STATE_KEY",
    "PERMISSION_OVERLAY_FILENAME",
    "PermissionRule",
    "append_persisted_rule",
    "effective_rules",
    "load_persisted_rules",
    "parse_rule",
    "read_approval_mode",
    "read_session_grants",
    "resolve_decision",
    "rule_matches",
    "write_session_grants",
]
