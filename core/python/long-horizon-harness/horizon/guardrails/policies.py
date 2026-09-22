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

"""Declarative tool-policy guardrail backed by a JSONL DSL.

Policies live in two places:

* A package-bundled seed (``default_policies.jsonl`` next to this
  module) — always loaded.
* An optional per-workspace overlay at
  ``<working_dir>/.lha/policies.jsonl``. Under the local backend it is
  hot-reloaded each call via mtime (edit the file mid-session, effect on
  the next tool call); under the sandbox backend it is read through the
  env interface — the overlay lives in the sandbox, not on the host — and
  cached per sandbox. The runtime guard uses ``load_policies_for_env``.

Five rule shapes are recognised on every entry; ``canonical_tool_name``
gates which tool the rule applies to.

* ``{"canonical_tool_name": "t", "destructive": true}`` — always block.
* ``{"canonical_tool_name": "t",
     "destructive_commands": {"<arg>": ["substring", ...]}}`` —
  block when ``args[<arg>]`` (string) contains any substring
  (case-insensitive).
* ``{"canonical_tool_name": "t",
     "destructive_commands_regex": {"<arg>": ["regex", ...]}}`` —
  block when any regex finds a match in ``args[<arg>]``
  (case-insensitive). Use this when substring matching is too
  blunt (anchors, alternation, word boundaries).
* ``{"canonical_tool_name": "t", "destructive_paths": ["/prefix/"]}`` —
  block when any path-shaped arg starts with one of the prefixes.
* ``{"canonical_tool_name": "t",
     "destructive_path_patterns": ["*/.ssh/*", ...]}`` —
  block when any path-shaped arg matches one of the fnmatch globs.
  Use this for per-user paths like ``*/.ssh/*`` that can't be
  expressed as a single prefix.

A block returns the same short-circuit dict shape other guards use:
``{"error": ..., "confirmation_required": True, "matched_rule": {...}}``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

from horizon.environment_context import active_environment
from horizon.guardrails._jsonl import iter_jsonl_objects
from horizon.guardrails._overlay import (
    is_local_env,
    overlay_cache_key,
    read_overlay_text,
)
from horizon.guardrails._regex_safety import safe_regex
from horizon.guardrails.command_safety import classify as classify_command
from horizon.guardrails.policy_grants import find_matching_grant
from horizon.tools import names
from horizon.tools.names import apply_tool_aliases

_logger = logging.getLogger(__name__)

DEFAULT_POLICIES_FILENAME = ".lha/policies.jsonl"
_DEFAULT_SEED_PATH = Path(__file__).with_name("default_policies.jsonl")

_PATH_ARG_NAMES: frozenset[str] = frozenset(
    {"path", "file_path", "target_path"}
)

_cache: dict[str, Any] = {"path": None, "mtime": None, "rules": None}


def _filter_unsafe_regexes(obj: dict[str, Any]) -> dict[str, Any]:
    """Filter out unsafe regex patterns from a policy rule's destructive_commands_regex."""
    destructive_regex = obj.get("destructive_commands_regex")
    if not isinstance(destructive_regex, dict):
        return obj
    filtered = {}
    for arg_name, patterns in destructive_regex.items():
        if not isinstance(patterns, list):
            filtered[arg_name] = patterns
            continue
        safe_patterns = []
        for pat in patterns:
            if isinstance(pat, str) and pat:
                if not safe_regex(pat):
                    _logger.warning(
                        "policies: dropping unsafe regex %r from overlay", pat
                    )
                else:
                    safe_patterns.append(pat)
            else:
                safe_patterns.append(pat)
        if safe_patterns:
            filtered[arg_name] = safe_patterns
    if filtered:
        obj = dict(obj)
        obj["destructive_commands_regex"] = filtered
    elif "destructive_commands_regex" in obj:
        obj = {
            k: v for k, v in obj.items() if k != "destructive_commands_regex"
        }
    return obj


def _alias_canonical_tool_name(obj: dict[str, Any]) -> dict[str, Any]:
    # default_policies.jsonl is data, not Python, so it can't reference
    # horizon.tools.names directly. Aliasing at load time keeps a rule
    # written under a since-renamed tool name pointed at the live tool.
    canonical = obj.get("canonical_tool_name")
    if not isinstance(canonical, str):
        return obj
    aliased = apply_tool_aliases(canonical)
    if aliased == canonical:
        return obj
    return {**obj, "canonical_tool_name": aliased}


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    return [
        _alias_canonical_tool_name(obj)
        for obj in iter_jsonl_objects(text, context="policies")
        if obj.get("canonical_tool_name")
    ]


def _read_default_seed() -> list[dict[str, Any]]:
    try:
        text = _DEFAULT_SEED_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return _parse_jsonl(text)


def _read_user_overlay(working_dir: Path) -> list[dict[str, Any]]:
    path = working_dir / DEFAULT_POLICIES_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        _logger.warning("policies: could not read %s", path)
        return []
    raw = _parse_jsonl(text)
    return [_filter_unsafe_regexes(obj) for obj in raw]


def load_policies(working_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return seed + user policies; user file is hot-reloaded by mtime."""
    seed = _read_default_seed()
    if working_dir is None:
        return list(seed)

    user_path = working_dir / DEFAULT_POLICIES_FILENAME
    try:
        mtime: float | None = user_path.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    except OSError:
        mtime = None

    cache_key = str(user_path)
    if (
        _cache["path"] == cache_key
        and _cache["mtime"] == mtime
        and _cache["rules"] is not None
        and mtime is not None
    ):
        return list(_cache["rules"])

    user = _read_user_overlay(working_dir)
    combined = list(seed) + list(user)
    _cache["path"] = cache_key
    _cache["mtime"] = mtime
    _cache["rules"] = combined
    return list(combined)


_overlay_cache: dict[str, list[dict[str, Any]]] = {}


async def load_policies_for_env(env: Any) -> list[dict[str, Any]]:
    """Seed + per-workspace ``.lha/policies.jsonl`` overlay, read through the right
    backend: local fs (mtime hot-reload) or the sandbox env interface (the overlay lives
    in the sandbox, not on the host). The bundled seed always loads from the
    package; sandbox overlay reads are cached per sandbox identity."""
    if env is None:
        return _read_default_seed()
    working_dir = getattr(env, "working_dir", None)
    if working_dir is None:
        return _read_default_seed()
    if is_local_env(env):
        return load_policies(working_dir)
    key = overlay_cache_key(env)
    cached = _overlay_cache.get(key)
    if cached is not None:
        return list(cached)
    text = await read_overlay_text(env, working_dir / DEFAULT_POLICIES_FILENAME)
    raw_overlay = _parse_jsonl(text)
    filtered_overlay = [_filter_unsafe_regexes(obj) for obj in raw_overlay]
    combined = _read_default_seed() + filtered_overlay
    _overlay_cache[key] = combined
    return list(combined)


def _active_env() -> Any | None:
    try:
        return active_environment()
    except RuntimeError:
        return None


def _evaluate(rule: dict[str, Any], tool_name: str, args: Any) -> str | None:
    if rule.get("canonical_tool_name") != tool_name:
        return None

    if rule.get("destructive") is True:
        return f"tool {tool_name!r} is marked destructive"

    destructive_commands = rule.get("destructive_commands")
    if isinstance(destructive_commands, dict) and isinstance(args, dict):
        for arg_name, patterns in destructive_commands.items():
            if not isinstance(patterns, list):
                continue
            value = args.get(arg_name)
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            for pat in patterns:
                if isinstance(pat, str) and pat and pat.lower() in lowered:
                    return f"destructive substring {pat!r} matched on arg {arg_name!r}"

    destructive_commands_regex = rule.get("destructive_commands_regex")
    if isinstance(destructive_commands_regex, dict) and isinstance(args, dict):
        for arg_name, patterns in destructive_commands_regex.items():
            if not isinstance(patterns, list):
                continue
            value = args.get(arg_name)
            if not isinstance(value, str):
                continue
            for pat in patterns:
                if not isinstance(pat, str) or not pat:
                    continue
                try:
                    if re.search(pat, value, re.IGNORECASE):
                        return f"destructive regex {pat!r} matched on arg {arg_name!r}"
                except re.error:
                    _logger.warning(
                        "policies: invalid regex %r in rule, skipping", pat
                    )

    destructive_paths = rule.get("destructive_paths")
    if isinstance(destructive_paths, list) and isinstance(args, dict):
        prefixes = [p for p in destructive_paths if isinstance(p, str) and p]
        for arg_name, value in args.items():
            if arg_name not in _PATH_ARG_NAMES:
                continue
            if not isinstance(value, str):
                continue
            for prefix in prefixes:
                if value.startswith(prefix):
                    return (
                        f"destructive path prefix {prefix!r} matched on "
                        f"arg {arg_name!r}"
                    )

    destructive_path_patterns = rule.get("destructive_path_patterns")
    if isinstance(destructive_path_patterns, list) and isinstance(args, dict):
        globs = [
            p for p in destructive_path_patterns if isinstance(p, str) and p
        ]
        for arg_name, value in args.items():
            if arg_name not in _PATH_ARG_NAMES:
                continue
            if not isinstance(value, str):
                continue
            for pat in globs:
                if fnmatch.fnmatchcase(value, pat):
                    return f"destructive path pattern {pat!r} matched on arg {arg_name!r}"

    return None


def _block(
    reason: str, rule: dict[str, Any], *, source: str = "policy"
) -> dict[str, Any]:
    return {
        "error": (
            f"tool call blocked by {source}: {reason}. If the user has "
            "explicitly approved this call, ask them to type "
            "`/grant <command>` to approve it for the rest of this "
            "session, then retry. Otherwise re-run with a narrower target "
            f"or ask the user to add a rule to {DEFAULT_POLICIES_FILENAME} "
            "(you cannot write it yourself, see the `policy` skill)."
        ),
        "confirmation_required": True,
        "matched_rule": rule,
    }


def _command_for(tool_name: str, args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    if tool_name == names.BASH and isinstance(args.get("command"), str):
        return args["command"]
    if (
        tool_name == names.PROCESS
        and args.get("action") == "write"
        and isinstance(args.get("data"), str)
    ):
        return args["data"]
    # process(action='spawn') is a shell execution path (bash's former
    # background=True moved here) — route it through command_safety exactly
    # like every other shell entry point.
    if (
        tool_name == names.PROCESS
        and args.get("action") == "spawn"
        and isinstance(args.get("command"), str)
    ):
        return args["command"]
    return None


async def policies_guard(
    *, tool: Any, args: Any, tool_context: Any, ask_is_deny: bool = False
) -> dict[str, Any] | None:
    """``before_tool_callback`` that consults the JSONL policy DSL."""
    tool_name = getattr(tool, "name", "") or ""
    if not tool_name:
        return None

    state = getattr(tool_context, "state", None)
    if state is not None and find_matching_grant(state, tool_name, args):
        return None

    rules = await load_policies_for_env(_active_env())
    for rule in rules:
        reason = _evaluate(rule, tool_name, args)
        if reason is not None:
            return _block(reason, rule, source="policy")

    # process(action="write") data payload also checked against bash JSONL rules
    if (
        tool_name == names.PROCESS
        and isinstance(args, dict)
        and args.get("action") == "write"
        and isinstance(args.get("data"), str)
    ):
        synthetic = {"command": args["data"]}
        for rule in rules:
            reason = _evaluate(rule, names.BASH, synthetic)
            if reason is not None:
                return _block(reason, rule, source="policy")

    # process(action="spawn") command also checked against bash JSONL rules —
    # same reasoning as the write branch above: this tool_name never matches
    # a rule's canonical_tool_name="bash" directly, so the destructive-command
    # seed (dd if=, fork bombs, cat ~/.ssh/, ...) would silently stop covering
    # a spawned command without this synthetic re-check.
    if (
        tool_name == names.PROCESS
        and isinstance(args, dict)
        and args.get("action") == "spawn"
        and isinstance(args.get("command"), str)
    ):
        synthetic = {"command": args["command"]}
        for rule in rules:
            reason = _evaluate(rule, names.BASH, synthetic)
            if reason is not None:
                return _block(reason, rule, source="policy")

    command = _command_for(tool_name, args)
    if command is not None:
        verdict = classify_command(command)
        if verdict is not None:
            tier, why = verdict
            if tier == "deny" or ask_is_deny:
                return _block(
                    why, {"command_safety": tier}, source="command_safety"
                )
            return None
    return None


__all__ = [
    "DEFAULT_POLICIES_FILENAME",
    "load_policies",
    "load_policies_for_env",
    "policies_guard",
]
