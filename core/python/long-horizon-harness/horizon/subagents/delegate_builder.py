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

"""Lazy builder: constructs a fresh child ``Agent`` per delegate call.

goal + context + skills go into the system prompt; toolsets expand into
the tool list; a blocklist strips anything the child must not call.

No caching: the prompt is goal-specific, so a per-call build is cheap
and removes a class of cache-staleness bugs (skill body updates, toolset
changes, blocklist tweaks).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from google.adk.agents import Agent
from google.adk.models import Gemini

from horizon.conversation.system_prompt import SECRETS_GUIDANCE
from horizon.environment_context import active_environment
from horizon.models.registry import MODEL_REGISTRY, ROBUST_RETRY_OPTIONS
from horizon.subagents.ask_parent import ask_parent
from horizon.subagents.child_guard import make_child_policy_guard
from horizon.subagents.profiles import ChildProfile, get_profile
from horizon.subagents.toolsets import (
    DEFAULT_TOOLSETS,
    resolve_tools_by_name,
    resolve_toolsets,
)
from horizon.tools import names
from horizon.tools.skill_loader import builtin_skills_root, walk_skill_dirs
from horizon.tools.skill_reload import host_mirror_dir

# Names of callables that must NEVER appear on a child agent.
# - subagent: no recursion (by construction).
# - clarify: no user-facing prompts from a child (no UI channel).
# - memory: no writes to the parent's curated memory bank, including reads.
# - load_skill: children may not mutate the parent's skill/extension
#   registries; requested skill bodies are inlined into the child's prompt
#   below instead, so the child never needs the catalog tools.
_BLOCKED_TOOL_NAMES = frozenset(
    {
        names.SUBAGENT,
        names.CLARIFY,
        names.MEMORY,
        names.LOAD_SKILL,
    }
)

_BASE_INSTRUCTION = (
    "You are a delegated worker. The parent agent has handed you a "
    "self-contained task. Do the work and return a concise summary of "
    "what you did, what you found, and any pointers the parent needs "
    "to act on (file paths, URLs, error messages). You have no memory "
    "of the parent's conversation: everything you need is in this "
    "prompt. For minor ambiguity, make the most reasonable interpretation "
    "and note the assumption in your summary. Only when a genuine blocker "
    "requires a decision the parent alone can make (ambiguous scope, a "
    "missing choice) call `ask_parent(question)` for a one-line answer and "
    "resume, you get one such escalation. If a tool call is blocked by a "
    "guard, report it in your summary rather than trying to work around it."
)

# Derived, not duplicated: a hand-kept copy drifts from the registry.
_ALLOWED_MODELS: frozenset[str] = frozenset(MODEL_REGISTRY)

# ADK requires the agent node name to be a valid Python identifier, so the
# prefix must be identifier-safe (no hyphens, no leading digit).
_NAME_SANITIZER = re.compile(r"[^a-zA-Z0-9_]+")
_NAME_PREFIX_MAX = 32


def _callable_name(tool: Any) -> str | None:
    return getattr(tool, "__name__", None) or getattr(tool, "name", None)


def _strip_blocked(tools: list[Any]) -> list[Any]:
    keep: list[Any] = []
    for tool in tools:
        if _callable_name(tool) in _BLOCKED_TOOL_NAMES:
            continue
        keep.append(tool)
    return keep


def load_session_skill_catalog() -> dict[str, Any]:
    # Read from the host mirror that bind_session_skills_callback already
    # populated this session; the env's working_dir is the in-container
    # /workspace under the sandbox backend, unreadable by this host process.
    try:
        env = active_environment()
        user_dir = host_mirror_dir(env.working_dir, getattr(env, "owner", None))
    except RuntimeError:
        user_dir = builtin_skills_root().parent / "_unbound_user_skills"
    return walk_skill_dirs(user_dir=user_dir, builtin_dir=builtin_skills_root())


async def _render_skill_block(name: str) -> str:
    catalog = load_session_skill_catalog()
    skill_obj = catalog.get(name)
    if skill_obj is None:
        return f"### Skill: {name}\n\n_Requested but not found in the skill registry._"
    return f"### Skill: {name}\n\n{skill_obj.instructions.strip()}"


def _render_inline_skill_block(skill: dict[str, str]) -> str:
    name = str(skill.get("name", "unnamed")).strip() or "unnamed"
    body = str(skill.get("body", "")).strip()
    return f"### Skill: {name}\n\n{body}"


def _render_json_directive() -> str:
    return (
        "## Output format\n\n"
        "Return ONLY a JSON object as your final response, no prose "
        "before or after, no markdown code fences."
    )


async def _assemble_instruction(
    goal: str,
    context: str,
    skill_names: list[str],
    *,
    caller_instructions: str | None = None,
    inline_skills: list[dict[str, str]] | None = None,
    json_directive: str | None = None,
) -> str:
    parts: list[str] = [_BASE_INSTRUCTION, SECRETS_GUIDANCE]
    parts.append(f"## Goal\n\n{goal.strip()}")
    if context and context.strip():
        parts.append(f"## Context\n\n{context.strip()}")
    skill_blocks: list[str] = []
    for n in skill_names:
        skill_blocks.append(await _render_skill_block(n))
    for s in inline_skills or []:
        skill_blocks.append(_render_inline_skill_block(s))
    if skill_blocks:
        parts.append("## Available skills\n\n" + "\n\n".join(skill_blocks))
    if caller_instructions and caller_instructions.strip():
        parts.append(f"## Caller instructions\n\n{caller_instructions.strip()}")
    if json_directive:
        parts.append(json_directive)
    return "\n\n".join(parts)


def _sanitize_name_prefix(raw: str) -> str:
    cleaned = _NAME_SANITIZER.sub("_", raw).strip("_")
    if not cleaned:
        return "delegate"
    if cleaned[0].isdigit():
        cleaned = f"d_{cleaned}"
    return cleaned[:_NAME_PREFIX_MAX]


async def build_child_agent(
    *,
    goal: str,
    context: str = "",
    toolsets: list[str] | None = None,
    skills: list[str] | None = None,
    model_name: str = "gemini-3.7-flash",
    tools: list[str] | None = None,
    instructions: str | None = None,
    inline_skills: list[dict[str, str]] | None = None,
    output_format: Literal["text", "json"] = "text",
    output_schema: dict[str, Any] | None = None,
    name: str | None = None,
    profile: str | None = None,
    parent_grants: list[dict[str, Any]] | None = None,
) -> Agent:
    """Materialize a child ``Agent`` ready to be handed to a Runner.

    ``toolsets`` defaults to ``DEFAULT_TOOLSETS`` (file + shell) only when
    no ``tools=[...]`` list is also supplied; passing ``tools`` alone
    means "give the child exactly these and nothing else". Unknown
    toolset or tool names raise ``KeyError``; unknown ``model_name``
    raises ``ValueError``. The dispatch layer catches both and surfaces
    a structured error to the parent.
    """
    if model_name not in _ALLOWED_MODELS:
        raise ValueError(
            f"Unknown model {model_name!r}. Allowed: {sorted(_ALLOWED_MODELS)}"
        )

    resolved_profile: ChildProfile | None = (
        get_profile(profile) if profile is not None else None
    )

    resolved_tools: list[Any] = []
    if toolsets is None and tools:
        resolved_tools = resolve_tools_by_name(tools)
    else:
        toolset_names = toolsets if toolsets else list(DEFAULT_TOOLSETS)
        resolved_tools = resolve_toolsets(toolset_names)
        if tools:
            seen_ids = {id(t) for t in resolved_tools}
            for extra in resolve_tools_by_name(tools):
                if id(extra) not in seen_ids:
                    resolved_tools.append(extra)
                    seen_ids.add(id(extra))
    if (
        resolved_profile is not None
        and resolved_profile.allowed_tool_names is not None
    ):
        allowed = resolved_profile.allowed_tool_names
        resolved_tools = [
            t for t in resolved_tools if _callable_name(t) in allowed
        ]
    final_tools = _strip_blocked(resolved_tools)
    # Always give the child its escalation channel, even under a profile
    # allowlist. child_guard exempts it; when no parent turn can answer
    # (background / headless) the tool returns a graceful no-parent result.
    final_tools.append(ask_parent)

    skill_names = list(skills or [])
    # When output_schema is present, ADK's _output_schema_processor injects
    # a SetModelResponseTool + its own instruction; we only need the
    # lightweight "return JSON" hint when the caller asked for JSON
    # without a schema.
    json_directive = (
        _render_json_directive()
        if output_format == "json" and not output_schema
        else None
    )
    instruction = await _assemble_instruction(
        goal,
        context,
        skill_names,
        caller_instructions=instructions,
        inline_skills=list(inline_skills or []),
        json_directive=json_directive,
    )

    prefix = _sanitize_name_prefix(name) if name else "delegate"
    agent_name = f"{prefix}_{uuid.uuid4().hex[:8]}"

    agent_kwargs: dict[str, Any] = {
        "name": agent_name,
        "model": Gemini(
            model=model_name,
            retry_options=ROBUST_RETRY_OPTIONS,
        ),
        "instruction": instruction,
        "tools": final_tools,
    }
    agent_kwargs["before_tool_callback"] = [
        make_child_policy_guard(
            parent_grants=parent_grants, profile=resolved_profile
        )
    ]
    if output_format == "json" and output_schema:
        agent_kwargs["output_schema"] = output_schema
    return Agent(**agent_kwargs)
