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

"""Two changes over ADK's stock ``SkillToolset`` (``google.adk.tools.skill_toolset``).

``HorizonSkillToolset`` replaces ADK's ~2 KB tutorial preamble with one line.
It operates on the DELTA ``super().process_llm_request()`` appends, never on
the whole accumulated ``system_instruction`` — by the time this runs in
production, ``system_instruction`` already holds the constant prefix
(``Agent.static_instruction``, ~6.9 KB), and splitting that whole string on
``<available_skills>`` would silently wipe it. See ``process_llm_request``
below for the exact mechanism and why the delta approach is required.

``LoadSkillTool`` merges ADK's ``LoadSkillTool``, ``LoadSkillResourceTool``,
and the standalone ``reload`` model tool into one: ``load_skill(skill_name)``
reads a skill's instructions, ``load_skill(skill_name, resource=...)`` reads
one of its bundled files, and ``load_skill(action='reload')`` refreshes the
skill catalog (no ``skill_name`` needed) — the same callable the ``/reload``
slash command drives (``horizon.commands.reload``), a separate, unaffected
surface. ``RunSkillScriptTool`` has no successor — a resource-loaded script
runs via ``bash`` instead. Deliberately narrower than ADK's
``LoadSkillResourceTool``: this tool does not reproduce its
``process_llm_request`` override, which injects a resource's raw bytes into
next turn's request when the resource is binary. No builtin skill ships a
binary resource today, and claiming "injected for you to analyze" (ADK's
message) while not injecting anything would be a tool that lies to the model
— see ``_load_resource`` below.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.skill_toolset import SkillToolset
from google.adk.utils import instructions_utils
from google.genai import types

_SHORT_SKILLS_PREAMBLE = (
    "\n\nSkills below extend your capabilities via SKILL.md instructions "
    "you load with load_skill before following them.\n\n"
)


class LoadSkillTool(BaseTool):
    """Loads a skill's instructions, or one of its bundled files."""

    def __init__(self, toolset: SkillToolset) -> None:
        super().__init__(
            name="load_skill",
            description=(
                "Loads a skill's SKILL.md instructions (skill_name "
                "required), or pass resource='references/x.md' (also "
                "assets/, scripts/) for one of its bundled files. "
                "action='reload' refreshes the skill catalog instead "
                "(no skill_name needed)."
            ),
        )
        self._toolset = toolset

    def _get_declaration(self) -> types.FunctionDeclaration | None:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["load", "reload"],
                        "description": (
                            "'load' (default): read a skill via skill_name. "
                            "'reload': refresh the skill catalog instead."
                        ),
                    },
                    "skill_name": {
                        "type": "string",
                        "description": (
                            "The name of the skill to load. Required unless "
                            "action='reload'."
                        ),
                    },
                    "resource": {
                        "type": "string",
                        "description": (
                            "Relative path to a bundled file, e.g. "
                            "'references/my_doc.md', 'assets/template.txt', "
                            "or 'scripts/setup.sh'. Omit to load the "
                            "skill's SKILL.md instructions instead."
                        ),
                    },
                },
            },
        )

    async def run_async(
        self, *, args: dict[str, Any], tool_context: Any
    ) -> Any:
        action = args.get("action") or "load"
        if action == "reload":
            from horizon.commands import reload as reload_skills

            return await reload_skills(tool_context=tool_context)
        if action != "load":
            return {
                "error": f"Unknown action {action!r}; use 'load' or 'reload'.",
                "error_code": "INVALID_ARGUMENTS",
            }

        skill_name = args.get("skill_name")
        if not skill_name:
            return {
                "error": "Argument 'skill_name' is required.",
                "error_code": "INVALID_ARGUMENTS",
            }

        try:
            skill = await self._toolset._get_or_fetch_skill(
                skill_name, tool_context.invocation_id
            )
        except Exception as e:
            return {
                "error": f"Failed to fetch skill '{skill_name}' from registry: {e}",
                "error_code": "REGISTRY_ERROR",
            }

        if not skill:
            return {
                "error": f"Skill '{skill_name}' not found.",
                "error_code": "SKILL_NOT_FOUND",
            }

        resource = args.get("resource")
        if resource:
            return await self._load_resource(
                skill, skill_name, resource, tool_context
            )

        # Record skill activation in agent state for tool resolution —
        # drives SkillToolset._resolve_additional_tools_from_state, which
        # exposes a skill's adk_additional_tools once it has been loaded.
        agent_name = tool_context.agent_name
        state_key = f"_adk_activated_skill_{agent_name}"
        activated_skills = list(tool_context.state.get(state_key) or [])
        if skill_name not in activated_skills:
            activated_skills.append(skill_name)
            tool_context.state[state_key] = activated_skills

        instructions = skill.instructions
        if skill.frontmatter.metadata.get("adk_inject_state"):
            instructions = await instructions_utils.inject_session_state(
                instructions, tool_context
            )

        return {
            "skill_name": skill_name,
            "instructions": instructions,
            "frontmatter": skill.frontmatter.model_dump(),
        }

    async def _load_resource(
        self, skill: Any, skill_name: str, file_path: str, tool_context: Any
    ) -> Any:
        content = None
        if file_path.startswith("references/"):
            content = skill.resources.get_reference(
                file_path[len("references/") :]
            )
        elif file_path.startswith("assets/"):
            content = skill.resources.get_asset(file_path[len("assets/") :])
        elif file_path.startswith("scripts/"):
            script = skill.resources.get_script(file_path[len("scripts/") :])
            if script is not None:
                content = script.src
        else:
            return {
                "error": (
                    "Path must start with 'references/', 'assets/', or 'scripts/'."
                ),
                "error_code": "INVALID_RESOURCE_PATH",
            }

        if content is None:
            # Invocation-scoped failure counter, mirroring ADK's
            # LoadSkillResourceTool: counts RESOURCE_NOT_FOUND across ALL
            # paths so the guard fires even when the model hallucinates a
            # different path on each retry. `temp:` keeps it out of durable
            # session storage; invocation_id isolates in-memory backends.
            counter_key = f"temp:_adk_skill_resource_not_found_count_{tool_context.invocation_id}"
            fail_count = int(tool_context.state.get(counter_key) or 0) + 1
            tool_context.state[counter_key] = fail_count
            if fail_count > 1:
                return {
                    "error": (
                        f"Resource '{file_path}' not found in skill "
                        f"'{skill_name}'. This is resource lookup failure "
                        f"#{fail_count} this invocation. Do not retry any "
                        "path — report the error to the user and stop."
                    ),
                    "error_code": "RESOURCE_NOT_FOUND_FATAL",
                }
            return {
                "error": (
                    f"Resource '{file_path}' not found in skill '{skill_name}'."
                ),
                "error_code": "RESOURCE_NOT_FOUND",
            }

        if isinstance(content, bytes):
            # Narrower than ADK's LoadSkillResourceTool on purpose (see the
            # module docstring): report the binary file honestly instead of
            # claiming an injection this tool does not perform.
            return {
                "skill_name": skill_name,
                "resource": file_path,
                "error": (
                    f"'{file_path}' is a binary file; this tool returns "
                    "text content only."
                ),
                "error_code": "BINARY_RESOURCE_UNSUPPORTED",
            }

        return {
            "skill_name": skill_name,
            "resource": file_path,
            "content": content,
        }

    def _detect_error_in_response(self, response: Any) -> str | None:
        """Telemetry hook: returns an error type if the response indicates an error."""
        if isinstance(response, dict) and response.get("error"):
            error_code = response.get("error_code")
            return error_code if error_code else "TOOL_ERROR"
        return None


class HorizonSkillToolset(SkillToolset):
    """``SkillToolset`` with a short skills preamble instead of ADK's tutorial."""

    async def process_llm_request(
        self, *, tool_context: Any, llm_request: Any
    ) -> None:
        # system_instruction is a shared accumulator already holding
        # static_instruction plus the project-context tier by the time this
        # runs; operate only on the delta THIS call appends, never the
        # accumulated whole, or a naive split-and-replace wipes everything
        # appended earlier (invisible to a test starting from a fresh
        # LlmRequest()).
        before = llm_request.config.system_instruction or ""
        await super().process_llm_request(
            tool_context=tool_context, llm_request=llm_request
        )
        after = llm_request.config.system_instruction or ""
        delta = after[len(before) :]

        index_start = delta.find("<available_skills>")
        if index_start < 0:
            # A silently empty index would strip every skill from the
            # model's view with no failure signal — raise instead.
            raise RuntimeError(
                "SkillToolset emitted no <available_skills> index"
            )

        llm_request.config.system_instruction = (
            before + _SHORT_SKILLS_PREAMBLE + delta[index_start:]
        )


__all__ = [
    "HorizonSkillToolset",
    "LoadSkillTool",
]
