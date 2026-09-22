# ruff: noqa
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

"""The ADK agent: ``root_agent`` + ``App``, with the full callback/tool/plugin
wiring. This is the canonical definition; ``horizon.fast_api_app`` serves it and
ADK's Runner entrypoint reads ``root_agent``/``app`` directly."""

import logging
import os

import google.auth

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App, ResumabilityConfig
from google.adk.apps._configs import EventsCompactionConfig
from google.adk.code_executors.agent_engine_sandbox_code_executor import (
    AgentEngineSandboxCodeExecutor,
)
from google.adk.models import BaseLlm, Gemini
from google.adk.tools.agent_tool import AgentTool
from horizon.memory.preload import HorizonPreloadMemoryTool

from horizon.commands.dispatcher import make_slash_command_dispatcher
from horizon.context.summarizer import HorizonSummarizer
from horizon.context.artifact_url_redaction import redact_artifact_urls_callback
from horizon.context.declaration_compaction import compact_tool_declarations
from horizon.context.schema_normalization import (
    normalize_tool_schemas_callback,
)
from horizon.context.tool_output_pruning import prune_tool_outputs_callback
from horizon.conversation.iteration_budget_plugin import IterationBudgetPlugin
from horizon.conversation.session_start import (
    on_session_start_callback,
)
from horizon.conversation.reminders import reminder_injection_callback
from horizon.conversation.system_prompt import (
    build_static_instruction,
    system_prompt_assembly_callback,
)
from horizon.guardrails import (
    GuardrailsPlugin,
    exfil_guard,
    policies_guard,
)
from horizon.guardrails.permission_guard import permission_guard
from horizon.telemetry.ui import (
    before_tool_log_callback,
    tool_call_log_callback,
)
from horizon.memory import auto_capture_callback, memory
from horizon.memory.review_fork import review_fork_callback
from horizon.memory.sibling_agent_plugin import SiblingAgentPlugin
from horizon.memory.skill_curator import skill_curator_callback
from horizon.memory.skill_telemetry import skill_telemetry_callback
from horizon.models import (
    MODEL_REGISTRY,
    DispatchingLlm,
    build_root_llm,
    select_model_callback,
)
from horizon.models.selector import resolve_model_name
from horizon.routines.tools import routine
from horizon.subagents.descriptions import subagent_description_callback
from horizon.subagents.subagent import subagent
from horizon.subagents.web_research import web_research_agent
from horizon.tools import (
    edit,
    search_files,
    write,
)
from horizon.tools.artifacts import artifact
from horizon.tools.read import ReadTool
from horizon.tools.clarify import clarify
from horizon.tools.skill_loader import build_skill_toolset, builtin_skills_root
from horizon.tools.skill_reload import (
    bind_session_skills_callback,
    bind_toolset,
)
from horizon.tools.processes.process import process
from horizon.tools.processes.terminal import bash

_logger = logging.getLogger(__name__)

# setdefault, not assignment: a consumer that pins a region/provider before
# building the agent keeps it (the project-wide default is global Vertex).
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        _, project_id = google.auth.default()
        if project_id:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
            _logger.warning(
                "GOOGLE_CLOUD_PROJECT unset; billing this run to the ADC default "
                "project %r. Set it explicitly to choose a different project.",
                project_id,
            )
    except Exception:  # no ADC (e.g. forked recipe imported offline) — leave project to env/explicit config
        _logger.debug(
            "google.auth.default() found no credentials; GOOGLE_CLOUD_PROJECT left as-is"
        )


SIBLING_AGENT_PLUGIN = SiblingAgentPlugin()

_slash_command_dispatcher = make_slash_command_dispatcher()


# Module-level toolset shared across sessions; ``bind_session_skills_callback``
# repoints it at the active session's ``working_dir/skills`` on each turn so
# ADK's auto-injected ``<available_skills>`` block reflects the current user.
# The initial bind covers built-ins only — a nonexistent placeholder user dir
# keeps the module importable in environments without a workspace (unit tests,
# eval harness import paths).
_SKILL_TOOLSET = build_skill_toolset(
    user_dir=builtin_skills_root().parent / "_unbound_user_skills",
    builtin_dir=builtin_skills_root(),
)
bind_toolset(
    _SKILL_TOOLSET,
    user_dir=builtin_skills_root().parent / "_unbound_user_skills",
    builtin_dir=builtin_skills_root(),
)


def _build_code_executor() -> AgentEngineSandboxCodeExecutor | None:
    resource = os.environ.get("CODE_SANDBOX_RESOURCE_NAME") or os.environ.get(
        "AGENT_ENGINE_RESOURCE_NAME"
    )
    if not resource:
        _logger.info(
            "code execution disabled: set AGENT_ENGINE_RESOURCE_NAME or "
            "CODE_SANDBOX_RESOURCE_NAME to enable the sandbox executor"
        )
        return None
    return AgentEngineSandboxCodeExecutor(resource_name=resource, stateful=True)


def _resolve_root_model(model: str | BaseLlm | None) -> BaseLlm:
    if model is None:
        return build_root_llm()
    if isinstance(model, str):
        if model not in MODEL_REGISTRY:
            raise ValueError(
                f"unknown model {model!r}; must be one of {sorted(MODEL_REGISTRY)}"
            )
        return DispatchingLlm(model=model, backends=MODEL_REGISTRY)
    return model


def _static_instruction_for(tools: list, has_code_executor: bool) -> str:
    # Built once here, at App-build time: the tool list and code executor are
    # both fixed by then (see build_static_instruction's docstring). A
    # BaseToolset (e.g. _SKILL_TOOLSET) has no .name of its own, so its
    # already-built ._tools (same private attribute skill_loader.py reaches
    # into) must be read for a name-keyed gate like SKILLS_GUIDANCE to fire.
    tool_names: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
        if name:
            tool_names.append(name)
            continue
        for inner in getattr(tool, "_tools", ()):
            inner_name = getattr(inner, "name", None)
            if inner_name:
                tool_names.append(inner_name)
    return build_static_instruction(
        tool_names=tool_names,
        # state=None: no session exists yet at import time, so this can't
        # honor a per-session /model override; matches select_model_callback's
        # own fallback, so the gate matches what a fresh session runs on.
        model_name=resolve_model_name(None),
        has_code_executor=has_code_executor,
    )


def _build_app_object() -> App:
    tools = [
        # memory covers both add (default) and search (former session_search)
        # actions.
        memory,
        # Stock PreloadMemoryTool appends <PAST_CONVERSATIONS> to
        # system_instruction, which the Gemini cache fingerprints; the block
        # changes every turn, so the cache never validates. This subclass
        # puts it in the cache-excluded contents tail instead.
        HorizonPreloadMemoryTool(),
        ReadTool(),
        write,
        edit,
        search_files,
        artifact,
        bash,
        process,
        subagent,
        # load_skill covers load/reload (reload_tool folded into it); the
        # /reload slash command still calls horizon.commands.reload directly.
        _SKILL_TOOLSET,
        routine,
        clarify,
        AgentTool(agent=web_research_agent),
    ]

    code_executor = _build_code_executor()

    root_agent = Agent(
        name="root_agent",
        model=_resolve_root_model(None),
        # Empty, not None: LlmAgent.instruction has no None in its type union
        # and rejects it; "" is falsy so ADK's instructions processor skips it
        # rather than demoting it into the uncached trailing-user-content tail.
        instruction="",
        static_instruction=_static_instruction_for(
            tools, has_code_executor=code_executor is not None
        ),
        # Compact declarations, scoped to our own tools: the env var that
        # selects them is process-wide and would re-render the tool surface
        # of any other ADK agent sharing this process.
        tools=compact_tool_declarations(tools),
        code_executor=code_executor,
        # Order in each list matters — callbacks run top-to-bottom; later entries
        # can read state mutated by earlier ones.
        before_agent_callback=[
            # Resolve workspace env, load user profile, seed memory preload cache.
            on_session_start_callback,
            # Repoint module-global skill toolset at this session's workspace dir.
            bind_session_skills_callback,
        ],
        before_model_callback=[
            # Resolve per-session model choice (state > LHA_ROOT_MODEL > default)
            # and stamp it onto llm_request.model so DispatchingLlm routes
            # correctly. Must run first — everything downstream may read the
            # model name.
            select_model_callback,
            # Reclaim context for free by zeroing old, large tool-result bodies
            # before the model (and ADK's token-threshold check) reads them.
            prune_tool_outputs_callback,
            # ADK's non-pydantic declaration path ships raw docstrings, source
            # indentation included. Strip it before the tools are serialized.
            normalize_tool_schemas_callback,
            # Strip the signed artifact URL from the model's view of the
            # `artifact` tool result (the client already got the link), so the
            # model can't paste the credentialed blob into its reply. Runs after
            # the converter has emitted the URL to the client.
            redact_artifact_urls_callback,
            # Intercept ``/<cmd>`` user turns and route to BUILTIN_COMMAND_REGISTRY.
            _slash_command_dispatcher,
            # Assemble the instruction tiers (stable + context). Volatile state
            # no longer rides the instruction — it ships via the reminder tail
            # below — but this still runs late so the cached prefix is set first.
            system_prompt_assembly_callback,
            # Append per-turn <system-reminder> Content to the rolling tail
            # (volatile state + near-budget warning + last-error nudge) instead
            # of the cached system prefix, so the prefix stays cache-stable.
            reminder_injection_callback,
            # Rewrite the subagent tool's description with the live skill
            # catalog + child profiles so the model's delegation menu stays
            # accurate per turn. Runs last so it sees the final tool set.
            subagent_description_callback,
        ],
        before_tool_callback=[
            # Stamps state['in_flight_tool_call'] so the chat spinner can show
            # "running <tool> · Ns" instead of opaque "thinking…".
            before_tool_log_callback,
            # Block outbound calls that carry secrets or reach non-allowlisted
            # hosts (data-exfiltration guard). Runs before policies_guard so a
            # secret-bearing call is stopped regardless of destructive rules.
            exfil_guard,
            # Hard-block destructive ops, surface confirmation-tier prompts.
            policies_guard,
            # Interactive per-operation approval (yes once / session / always /
            # decline). Runs last so the security guards above keep hard-deny
            # power; this gate only decides allow/ask for the survivors.
            permission_guard,
        ],
        after_tool_callback=[
            # Bumps view/edit/error counters used by skill_curator_callback.
            skill_telemetry_callback,
            # Emits structured tool-call event for the web UI live log.
            tool_call_log_callback,
        ],
        after_agent_callback=[
            # Flush just-finished session to Memory Bank (throttled).
            auto_capture_callback,
            # Promote/demote skills based on session telemetry counters.
            skill_curator_callback,
            # Spawn forked judge-agent to surface durable facts (throttled).
            review_fork_callback,
        ],
    )

    plugins = [
        IterationBudgetPlugin(max_tool_calls_per_iteration=200),
        SIBLING_AGENT_PLUGIN,
        GuardrailsPlugin(),
    ]

    return App(
        root_agent=root_agent,
        name="app",
        plugins=plugins,
        resumability_config=ResumabilityConfig(is_resumable=True),
        context_cache_config=ContextCacheConfig(
            # Gemini's own per-model floor (gemini_context_cache_manager.py's
            # _minimum_cache_tokens) is hardcoded to 4096 for any gemini-3*
            # model, so 2048 here was dead config for horizon's default
            # model; 4096 documents the real floor instead of a smaller,
            # never-binding number.
            min_tokens=4096,
            ttl_seconds=1800,
            cache_intervals=10,
        ),
        events_compaction_config=EventsCompactionConfig(
            # Seed value only — overwritten each turn by select_model_callback to
            # the active model's percentage-of-window threshold (see
            # horizon/context/compaction_threshold.py).
            token_threshold=750_000,
            event_retention_size=20,
            # Sliding-window trigger — also fires deterministically every N
            # user-initiated invocations so token-light long sessions still
            # get rolling summaries.
            compaction_interval=8,
            overlap_size=2,
            summarizer=HorizonSummarizer(llm=Gemini(model="gemini-3.7-flash")),
        ),
    )


app = _build_app_object()
root_agent = app.root_agent
