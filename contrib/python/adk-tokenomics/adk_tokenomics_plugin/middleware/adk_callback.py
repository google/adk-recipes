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

# ruff: noqa: S110
"""
Tokenomics SDK - Google ADK Plugin & Callbacks
Plug-and-play middleware that automatically captures ADK turn telemetry without manual instrumentation.
"""

import atexit
import inspect
import json
import math
import os
import tempfile
import time
from collections import OrderedDict
from typing import Any

from adk_tokenomics_plugin.core.tracker import TokenTracker

try:
    from google.adk.plugins import BasePlugin as _ADKBasePlugin
except Exception:

    class _ADKBasePlugin:  # type: ignore
        def __init__(self, name: str = "adk_tokenomics_plugin"):
            self.name = name


class _AwaitableNone:
    """Awaitable None-like object so ADK callbacks work in both `await callback(...)` (PluginManager) and sync calls."""

    __hash__: Any = None

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: Any) -> bool:
        return other is None or isinstance(other, _AwaitableNone)

    def __await__(self):
        if False:
            yield None


_AWAITABLE_NONE = _AwaitableNone()
_MAX_TRACKED_INVOCATIONS = 1000
_MAX_SEEN_USAGE_IDS = 5000


class ADKTokenomicsPlugin(_ADKBasePlugin):
    """
    Drop-in Google ADK Plugin for automated tokenomics telemetry.
    Implements native ADK BasePlugin hooks (`before_model_callback`,
    `after_model_callback`, `after_tool_callback`, `on_event_callback`)
    as well as direct `on_event()` invocation.

    Usage:
        from adk_tokenomics_plugin.middleware.adk_callback import ADKTokenomicsPlugin

        plugin = ADKTokenomicsPlugin(app_name="customer_agent")
        # Attach to ADK Runner(plugins=[plugin]) or Agent callbacks
    """

    def __init__(
        self,
        tracker: TokenTracker | None = None,
        app_name: str = "adk_agent",
        tower: Any | None = None,
        auto_export_path: str | None = None,
    ):
        try:
            super().__init__(name="adk_tokenomics_plugin")
        except Exception:
            self.name = "adk_tokenomics_plugin"
        resolved_tracker = tracker or (
            getattr(tower, "tracker", None) if tower is not None else None
        )
        self.tracker = resolved_tracker or TokenTracker()
        self.app_name = (
            app_name
            if app_name != "adk_agent"
            else (os.environ.get("TOKENOMICS_APP_NAME") or app_name)
        )
        self.auto_export_path = auto_export_path or os.environ.get(
            "TOKENOMICS_AUTO_EXPORT_PATH"
        )
        self._active_export_session_id: str | None = None
        self._last_exported_file_session_id: str | None = None
        self._last_exported_file_path: str | None = None
        self._last_exported_file_stat: tuple[int, int, int] | None = None
        self._unfinalized_sessions: set[str] = set()
        self._hydrated_sessions: set[str] = set()
        self._turn_start_time: float | None = None
        self._agent_start_times: dict[str, float] = {}
        self._last_user_query: str = ""
        self._last_request_model: str | None = None
        self._pending_tools: list[str] = []
        self._pending_skills: list[str] = []
        self._last_model_tools: list[str] = []
        self._consumed_model_tools: list[str] = []
        self._awaiting_post_model_tools: bool = False
        self._turn_states: OrderedDict[
            tuple[str | None, str | None], dict[str, Any]
        ] = OrderedDict()
        self._seen_usage_ids: OrderedDict[int, Any] = OrderedDict()
        self._seen_tool_call_ids: OrderedDict[
            tuple[str, str | None, str | None, str, str], bool
        ] = OrderedDict()
        self._recent_model_signatures: OrderedDict[
            tuple[str, str | None, str | None], tuple[int, int, int, int]
        ] = OrderedDict()
        self._agent_tool_children: set[str] = set()
        self._active_agent_tool_scopes: dict[
            str, list[dict[str, str | None]]
        ] = {}
        self._child_session_to_parent: OrderedDict[
            str, tuple[str, str | None]
        ] = OrderedDict()
        self._active_root_session_id: str | None = None
        self._active_root_invocation_id: str | None = None
        if self.auto_export_path:
            atexit.register(self._atexit_finalize_session)

    @classmethod
    def _collect_agent_tree_names(
        cls, agent_obj: Any, _visited: set[int] | None = None
    ) -> set[str]:
        """Collects the names of `agent_obj` and all descendant `sub_agents` or nested `AgentTool` agents."""
        names: set[str] = set()
        if agent_obj is None:
            return names
        visited = _visited if _visited is not None else set()
        obj_id = id(agent_obj)
        if obj_id in visited:
            return names
        visited.add(obj_id)

        name = getattr(agent_obj, "name", None)
        if name:
            names.add(str(name))
        for sub in getattr(agent_obj, "sub_agents", None) or []:
            names.update(cls._collect_agent_tree_names(sub, visited))
        for tool in getattr(agent_obj, "tools", None) or []:
            wrapped = getattr(tool, "agent", None)
            if wrapped is not None:
                names.update(cls._collect_agent_tree_names(wrapped, visited))
        return names

    def _discover_agent_tools(
        self, agent_obj: Any, _visited: set[int] | None = None
    ) -> None:
        """Discovers any sub-agents wrapped inside `AgentTool` across the agent hierarchy."""
        if agent_obj is None:
            return
        visited = _visited if _visited is not None else set()
        obj_id = id(agent_obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        for tool in getattr(agent_obj, "tools", None) or []:
            wrapped = getattr(tool, "agent", None)
            if wrapped is not None:
                self._agent_tool_children.update(
                    self._collect_agent_tree_names(wrapped)
                )
                self._discover_agent_tools(wrapped, visited)
        for sub in getattr(agent_obj, "sub_agents", None) or []:
            self._discover_agent_tools(sub, visited)

    def attach_to_agent(
        self, agent_obj: Any, _visited: set[int] | None = None
    ) -> Any:
        """Recursively attaches plugin callbacks to `agent_obj`, its `sub_agents`, and any `AgentTool`-wrapped agents."""
        if agent_obj is None:
            return agent_obj
        visited = _visited if _visited is not None else set()
        obj_id = id(agent_obj)
        if obj_id in visited:
            return agent_obj
        visited.add(obj_id)

        self._discover_agent_tools(agent_obj)
        for attr, cb in (
            ("before_model_callback", self.before_model_callback),
            ("after_model_callback", self.after_model_callback),
            ("before_tool_callback", self.before_tool_callback),
            ("after_tool_callback", self.after_tool_callback),
        ):
            if (
                hasattr(agent_obj, attr)
                and getattr(agent_obj, attr, None) is None
            ):
                try:
                    setattr(agent_obj, attr, cb)
                except Exception:
                    pass

        for sub in getattr(agent_obj, "sub_agents", None) or []:
            self.attach_to_agent(sub, visited)
        for tool in getattr(agent_obj, "tools", None) or []:
            wrapped = getattr(tool, "agent", None)
            if wrapped is not None:
                self.attach_to_agent(wrapped, visited)
        return agent_obj

    def _resolve_effective_session(
        self,
        raw_session_id: str,
        raw_invocation_id: str | None,
        agent_name: str | None,
    ) -> tuple[str, str | None]:
        """Maps ephemeral child `AgentTool` sessions back to the parent session and parent invocation ID."""
        if raw_session_id in self._child_session_to_parent:
            self._child_session_to_parent.move_to_end(raw_session_id)
            return self._child_session_to_parent[raw_session_id]

        if agent_name:
            scopes = self._active_agent_tool_scopes.get(agent_name)
            if scopes:
                top = scopes[-1]
                p_sid = top.get("parent_session_id") or raw_session_id
                p_inv = top.get("parent_invocation_id") or raw_invocation_id
                if raw_session_id not in ("default_session", p_sid):
                    self._child_session_to_parent[raw_session_id] = (
                        p_sid,
                        p_inv,
                    )
                    while (
                        len(self._child_session_to_parent)
                        > _MAX_TRACKED_INVOCATIONS
                    ):
                        self._child_session_to_parent.popitem(last=False)
                return p_sid, p_inv

            if (
                agent_name in self._agent_tool_children
                and self._active_root_session_id
            ):
                p_sid = self._active_root_session_id
                p_inv = self._active_root_invocation_id or raw_invocation_id
                if raw_session_id not in ("default_session", p_sid):
                    self._child_session_to_parent[raw_session_id] = (
                        p_sid,
                        p_inv,
                    )
                    while (
                        len(self._child_session_to_parent)
                        > _MAX_TRACKED_INVOCATIONS
                    ):
                        self._child_session_to_parent.popitem(last=False)
                return p_sid, p_inv

        if raw_session_id != "default_session" and (
            not agent_name or agent_name not in self._agent_tool_children
        ):
            self._active_root_session_id = raw_session_id
            if raw_invocation_id:
                self._active_root_invocation_id = raw_invocation_id
        return raw_session_id, raw_invocation_id

    @staticmethod
    def _extract_context_keys(
        ctx: Any,
    ) -> tuple[str, str | None, str | None]:
        """Extracts (session_id, invocation_id, agent_name) from an ADK CallbackContext, ToolContext, or InvocationContext."""
        if ctx is None:
            return "default_session", None, None
        session = getattr(ctx, "session", None)
        session_id = (
            getattr(session, "id", None)
            or getattr(ctx, "session_id", None)
            or "default_session"
        )
        invocation_id = getattr(ctx, "invocation_id", None)
        agent_name = getattr(ctx, "agent_name", None)
        if not agent_name:
            agent_obj = getattr(ctx, "agent", None)
            agent_name = (
                getattr(agent_obj, "name", None)
                if agent_obj is not None
                else None
            )
        return (
            str(session_id),
            str(invocation_id) if invocation_id else None,
            str(agent_name) if agent_name else None,
        )

    @staticmethod
    def _extract_orchestration_flow(
        ctx: Any, agent_tool_children: set[str] | None = None
    ) -> str | None:
        """Inspects ADK agent.parent_agent hierarchy and AgentTool membership at runtime to classify Root, AgentTool, Parallel, Loop, or Sequential flow."""
        if ctx is None:
            return None
        inv_ctx = getattr(ctx, "_invocation_context", None) or getattr(
            ctx, "invocation_context", None
        )
        agent_obj = getattr(ctx, "agent", None) or (
            getattr(inv_ctx, "agent", None) if inv_ctx is not None else None
        )
        agent_name = getattr(ctx, "agent_name", None) or (
            getattr(agent_obj, "name", None) if agent_obj is not None else None
        )
        is_agent_tool_child = bool(
            agent_name
            and agent_tool_children
            and str(agent_name) in agent_tool_children
        )
        if agent_obj is None:
            return "AgentTool" if is_agent_tool_child else None
        has_parent_attr = hasattr(agent_obj, "parent_agent")
        parent = getattr(agent_obj, "parent_agent", None)
        if parent is None:
            if is_agent_tool_child:
                return "AgentTool"
            return "Root" if has_parent_attr else None

        ancestors: list[str] = []
        cur = parent
        depth = 0
        while cur is not None and depth < 8:
            cls_name = type(cur).__name__
            ancestors.append(cls_name)
            cur = getattr(cur, "parent_agent", None)
            depth += 1

        is_parallel = any("Parallel" in a for a in ancestors)
        is_loop = any("Loop" in a for a in ancestors)
        is_seq = any("Sequential" in a for a in ancestors)

        if is_parallel and is_loop:
            return (
                "AgentTool • Parallel • Loop"
                if is_agent_tool_child
                else "Parallel • Loop"
            )
        if is_parallel:
            return "AgentTool • Parallel" if is_agent_tool_child else "Parallel"
        if is_loop:
            return "AgentTool • Loop" if is_agent_tool_child else "Loop"
        if is_seq:
            return (
                "AgentTool • Sequential"
                if is_agent_tool_child
                else "Sequential"
            )
        return "AgentTool" if is_agent_tool_child else "Sub-Agent"

    def _get_or_create_turn_state(
        self, invocation_id: str | None, agent_name: str | None
    ) -> dict[str, Any]:
        key = (invocation_id, agent_name)
        if key in self._turn_states:
            self._turn_states.move_to_end(key)
            return self._turn_states[key]
        state: dict[str, Any] = {
            "start_time": None,
            "user_query": "",
            "request_model": None,
            "orchestration_flow": None,
            "parent_session_id": None,
            "parent_invocation_id": None,
            "pending_tools": [],
            "pending_skills": [],
            "last_model_tools": [],
            "consumed_model_tools": [],
            "awaiting_post_model_tools": False,
        }
        self._turn_states[key] = state
        while len(self._turn_states) > _MAX_TRACKED_INVOCATIONS:
            self._turn_states.popitem(last=False)
        return state

    def _resolve_turn_state(
        self, invocation_id: str | None, agent_name: str | None
    ) -> dict[str, Any] | None:
        for candidate_key in (
            (invocation_id, agent_name),
            (invocation_id, None),
            (None, agent_name),
        ):
            if (
                candidate_key != (None, None)
                and candidate_key in self._turn_states
            ):
                return self._turn_states[candidate_key]
        return None

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        """Extracts plain text from a GenAI Content object, dict, or string."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            parts = content.get("parts") or []
            texts = [
                str(
                    p.get("text", "")
                    if isinstance(p, dict)
                    else getattr(p, "text", "")
                )
                for p in parts
            ]
            joined = " ".join(t for t in texts if t).strip()
            return joined or str(content.get("text") or "").strip()
        parts = getattr(content, "parts", None) or []
        texts = [
            str(getattr(p, "text", ""))
            for p in parts
            if getattr(p, "text", None)
        ]
        if texts:
            return " ".join(texts).strip()
        raw_txt = getattr(content, "text", None)
        return str(raw_txt).strip() if raw_txt else ""

    @classmethod
    def _extract_user_query_from_context(cls, ctx: Any) -> str:
        """Extracts the active user prompt from ADK CallbackContext / InvocationContext (`user_content` or session events)."""
        if ctx is None:
            return ""
        user_content = getattr(ctx, "user_content", None)
        if user_content is not None:
            txt = cls._extract_text_from_content(user_content)
            if txt:
                return txt
        inv_ctx = getattr(ctx, "_invocation_context", None) or getattr(
            ctx, "invocation_context", None
        )
        if inv_ctx is not None:
            uc = getattr(inv_ctx, "user_content", None)
            if uc is not None:
                txt = cls._extract_text_from_content(uc)
                if txt:
                    return txt
        session = getattr(ctx, "session", None)
        events = (
            getattr(session, "events", None) if session is not None else None
        )
        if isinstance(events, list):
            for ev in reversed(events):
                author = getattr(ev, "author", None) or (
                    ev.get("author") if isinstance(ev, dict) else None
                )
                ev_content = getattr(ev, "content", None) or (
                    ev.get("content") if isinstance(ev, dict) else None
                )
                role = getattr(ev_content, "role", None) or (
                    ev_content.get("role")
                    if isinstance(ev_content, dict)
                    else None
                )
                if author == "user" or role == "user":
                    txt = cls._extract_text_from_content(ev_content)
                    if txt and not txt.startswith("For context:"):
                        return txt
        return ""

    def on_user_message_callback(
        self,
        *,
        invocation_context: Any = None,
        user_message: Any = None,
        **kwargs,
    ) -> Any:
        """ADK lifecycle hook invoked immediately when a user message is received."""
        self._discover_agent_tools(getattr(invocation_context, "agent", None))
        raw_sid, invocation_id, agent_name = self._extract_context_keys(
            invocation_context
        )
        eff_sid, parent_inv_id = self._resolve_effective_session(
            raw_sid, invocation_id, agent_name
        )
        parsed_query = self._extract_text_from_content(
            user_message
        ) or self._extract_user_query_from_context(invocation_context)
        if parsed_query:
            self._last_user_query = parsed_query
            if invocation_id or agent_name:
                state = self._get_or_create_turn_state(
                    invocation_id, agent_name
                )
                state["user_query"] = parsed_query
                state["parent_session_id"] = eff_sid
                state["parent_invocation_id"] = parent_inv_id
                if invocation_id and agent_name:
                    inv_state = self._get_or_create_turn_state(
                        invocation_id, None
                    )
                    inv_state["user_query"] = parsed_query
                    inv_state["parent_session_id"] = eff_sid
                    inv_state["parent_invocation_id"] = parent_inv_id
        return _AWAITABLE_NONE

    def before_run_callback(
        self, *, invocation_context: Any = None, **kwargs
    ) -> Any:
        """ADK lifecycle hook invoked at the start of an invocation run."""
        self._discover_agent_tools(getattr(invocation_context, "agent", None))
        raw_sid, invocation_id, agent_name = self._extract_context_keys(
            invocation_context
        )
        eff_sid, parent_inv_id = self._resolve_effective_session(
            raw_sid, invocation_id, agent_name
        )
        parsed_query = self._extract_user_query_from_context(invocation_context)
        if parsed_query:
            self._last_user_query = parsed_query
            if invocation_id or agent_name:
                state = self._get_or_create_turn_state(
                    invocation_id, agent_name
                )
                state["user_query"] = parsed_query
                state["parent_session_id"] = eff_sid
                state["parent_invocation_id"] = parent_inv_id
                if invocation_id and agent_name:
                    inv_state = self._get_or_create_turn_state(
                        invocation_id, None
                    )
                    inv_state["user_query"] = parsed_query
                    inv_state["parent_session_id"] = eff_sid
                    inv_state["parent_invocation_id"] = parent_inv_id
        return _AWAITABLE_NONE

    def before_model_callback(
        self, callback_context: Any = None, llm_request: Any = None, **kwargs
    ) -> Any:
        """ADK lifecycle hook invoked prior to LLM request execution."""
        now = time.time()
        inv_ctx = getattr(
            callback_context, "_invocation_context", None
        ) or getattr(callback_context, "invocation_context", None)
        agent_obj = getattr(callback_context, "agent", None) or (
            getattr(inv_ctx, "agent", None) if inv_ctx is not None else None
        )
        self._discover_agent_tools(agent_obj)
        raw_sid, invocation_id, agent_name = self._extract_context_keys(
            callback_context
        )
        eff_sid, parent_inv_id = self._resolve_effective_session(
            raw_sid, invocation_id, agent_name
        )
        self._turn_start_time = now
        if agent_name:
            self._agent_start_times[agent_name] = now

        parsed_model: str | None = None
        parsed_query: str = self._extract_user_query_from_context(
            callback_context
        )
        fallback_context_query: str = ""
        if llm_request is not None:
            req_model = getattr(llm_request, "model", None)
            if req_model:
                parsed_model = str(
                    getattr(req_model, "model", None) or req_model
                )
                self._last_request_model = parsed_model
            if not parsed_query:
                contents = getattr(llm_request, "contents", None) or []
                for content in reversed(contents):
                    role = getattr(content, "role", None) or (
                        content.get("role")
                        if isinstance(content, dict)
                        else "user"
                    )
                    if role in ("user", None):
                        candidate_txt = self._extract_text_from_content(content)
                        if candidate_txt:
                            if candidate_txt.startswith("For context:"):
                                if not fallback_context_query:
                                    fallback_context_query = candidate_txt
                                continue
                            parsed_query = candidate_txt
                            break
                if not parsed_query and fallback_context_query:
                    parsed_query = fallback_context_query

        if parsed_query:
            self._last_user_query = parsed_query

        self._last_model_tools.clear()
        self._consumed_model_tools.clear()
        self._awaiting_post_model_tools = False
        flow_type = self._extract_orchestration_flow(
            callback_context, self._agent_tool_children
        )
        if invocation_id or agent_name:
            state = self._get_or_create_turn_state(invocation_id, agent_name)
            state["start_time"] = now
            state["last_model_tools"].clear()
            state["consumed_model_tools"].clear()
            state["awaiting_post_model_tools"] = False
            state["parent_session_id"] = eff_sid
            state["parent_invocation_id"] = parent_inv_id
            if flow_type:
                state["orchestration_flow"] = flow_type
            if parsed_model:
                state["request_model"] = parsed_model
            if parsed_query:
                state["user_query"] = parsed_query
            if invocation_id and agent_name:
                inv_state = self._get_or_create_turn_state(invocation_id, None)
                inv_state["last_model_tools"].clear()
                inv_state["consumed_model_tools"].clear()
                inv_state["awaiting_post_model_tools"] = False
                inv_state["parent_session_id"] = eff_sid
                inv_state["parent_invocation_id"] = parent_inv_id
                if inv_state["start_time"] is None:
                    inv_state["start_time"] = now
                if flow_type and not inv_state["orchestration_flow"]:
                    inv_state["orchestration_flow"] = flow_type
                if parsed_model and not inv_state["request_model"]:
                    inv_state["request_model"] = parsed_model
                if parsed_query and not inv_state["user_query"]:
                    inv_state["user_query"] = parsed_query
        return _AWAITABLE_NONE

    def before_tool_callback(
        self,
        tool: Any = None,
        tool_args: dict[str, Any] | None = None,
        tool_context: Any = None,
        *,
        args: dict[str, Any] | None = None,
        **kwargs,
    ) -> Any:
        """ADK lifecycle hook invoked prior to tool execution; links AgentTool child runs to the parent session."""
        if tool_args is None and args is not None:
            tool_args = args
        raw_sid, raw_inv_id, caller_agent = self._extract_context_keys(
            tool_context
        )
        parent_sid, parent_inv_id = self._resolve_effective_session(
            raw_sid, raw_inv_id, caller_agent
        )
        wrapped_agent = getattr(tool, "agent", None)
        if wrapped_agent is not None:
            target_names = self._collect_agent_tree_names(wrapped_agent)
            self._agent_tool_children.update(target_names)
            self._discover_agent_tools(wrapped_agent)
            fc_id = str(getattr(tool_context, "function_call_id", "") or "")
            scope_entry = {
                "parent_session_id": parent_sid,
                "parent_invocation_id": parent_inv_id,
                "function_call_id": fc_id or None,
            }
            for t_name in target_names:
                scopes = self._active_agent_tool_scopes.setdefault(t_name, [])
                if (
                    fc_id
                    and scopes
                    and scopes[-1].get("function_call_id") == fc_id
                ):
                    continue
                scopes.append(scope_entry)
        return _AWAITABLE_NONE

    def _pop_agent_tool_scope(self, tool: Any) -> None:
        wrapped_agent = getattr(tool, "agent", None)
        if wrapped_agent is not None:
            for t_name in self._collect_agent_tree_names(wrapped_agent):
                scopes = self._active_agent_tool_scopes.get(t_name)
                if scopes:
                    scopes.pop()

    def on_tool_error_callback(
        self,
        tool: Any = None,
        tool_args: dict[str, Any] | None = None,
        tool_context: Any = None,
        error: Exception | None = None,
        *,
        args: dict[str, Any] | None = None,
        **kwargs,
    ) -> Any:
        """Cleans up any active AgentTool scope if a tool raises an exception."""
        self._pop_agent_tool_scope(tool)
        return _AWAITABLE_NONE

    def _attribute_skill_to_turn_history(
        self,
        skill_name: str,
        invocation_id: str | None,
        agent_name: str | None,
    ) -> None:
        """Attributes an activated skill to the calling agent's turn record (safe for ParallelAgent interleaving)."""
        if not skill_name or not self.tracker.turns_history:
            return
        appended_session_id: str | None = None
        with self.tracker._lock:
            target_rec: dict[str, Any] | None = None
            if agent_name:
                if invocation_id:
                    for rec_cand in reversed(self.tracker.turns_history):
                        rec_agent = rec_cand.get("sub_agent") or (
                            rec_cand.get("raw_json") or {}
                        ).get("sub_agent")
                        if (
                            rec_agent == agent_name
                            and rec_cand.get("invocation_id") == invocation_id
                        ):
                            target_rec = rec_cand
                            break
                if target_rec is None:
                    for rec_cand in reversed(self.tracker.turns_history):
                        rec_agent = rec_cand.get("sub_agent") or (
                            rec_cand.get("raw_json") or {}
                        ).get("sub_agent")
                        if rec_agent == agent_name:
                            target_rec = rec_cand
                            break
            elif invocation_id:
                for rec_cand in reversed(self.tracker.turns_history):
                    if rec_cand.get("invocation_id") == invocation_id:
                        target_rec = rec_cand
                        break
            else:
                target_rec = self.tracker.turns_history[-1]

            if target_rec is not None and isinstance(
                target_rec.get("skills_active"), list
            ):
                if skill_name not in target_rec["skills_active"]:
                    target_rec["skills_active"].append(skill_name)
                    appended_session_id = str(
                        target_rec.get("session_id") or "default_session"
                    )

        if appended_session_id and self.auto_export_path:
            try:
                self._export_and_rotate_session(appended_session_id)
            except Exception:
                pass

    def after_tool_callback(
        self,
        tool: Any = None,
        tool_args: dict[str, Any] | None = None,
        tool_context: Any = None,
        result: Any = None,
        *,
        args: dict[str, Any] | None = None,
        tool_response: Any = None,
        **kwargs,
    ) -> Any:
        """ADK lifecycle hook invoked after tool execution to track tool and skill usage."""
        if tool_args is None and args is not None:
            tool_args = args
        if result is None and tool_response is not None:
            result = tool_response

        self._pop_agent_tool_scope(tool)
        tool_name = str(
            getattr(tool, "name", None)
            or getattr(tool, "__name__", str(tool or "tool"))
        )
        skill_name: str | None = None
        if (
            "skill" in tool_name.lower()
            and "unload" not in tool_name.lower()
            and isinstance(tool_args, dict)
        ):
            raw_skill = (
                tool_args.get("skill_name")
                or tool_args.get("name")
                or tool_args.get("skill")
            )
            if raw_skill:
                skill_name = str(raw_skill)

        raw_sid, invocation_id, agent_name = self._extract_context_keys(
            tool_context
        )
        fc_id = (
            getattr(tool_context, "function_call_id", None)
            if tool_context is not None
            else None
        )
        if fc_id:
            dedup_key = (
                raw_sid,
                invocation_id,
                agent_name,
                tool_name,
                str(fc_id),
            )
            if dedup_key in self._seen_tool_call_ids:
                self._seen_tool_call_ids.move_to_end(dedup_key)
                return _AWAITABLE_NONE
            self._seen_tool_call_ids[dedup_key] = True
            while len(self._seen_tool_call_ids) > _MAX_SEEN_USAGE_IDS:
                self._seen_tool_call_ids.popitem(last=False)

        if invocation_id or agent_name:
            state = self._get_or_create_turn_state(invocation_id, agent_name)
            if tool_name in state["last_model_tools"]:
                state["last_model_tools"].remove(tool_name)
                state["consumed_model_tools"].append(tool_name)
                if skill_name:
                    self._attribute_skill_to_turn_history(
                        skill_name, invocation_id, agent_name
                    )
                return _AWAITABLE_NONE
            if (
                state.get("awaiting_post_model_tools")
                and tool_name in state["consumed_model_tools"]
            ):
                # Duplicate callback when plugin is registered on both Runner and Agent via attach_to_agent
                if skill_name:
                    self._attribute_skill_to_turn_history(
                        skill_name, invocation_id, agent_name
                    )
                return _AWAITABLE_NONE
            if tool_name not in state["pending_tools"]:
                state["pending_tools"].append(tool_name)
            if skill_name and skill_name not in state["pending_skills"]:
                state["pending_skills"].append(skill_name)
        else:
            if tool_name in self._last_model_tools:
                self._last_model_tools.remove(tool_name)
                self._consumed_model_tools.append(tool_name)
                if skill_name:
                    self._attribute_skill_to_turn_history(
                        skill_name, None, None
                    )
                return _AWAITABLE_NONE
            if (
                self._awaiting_post_model_tools
                and tool_name in self._consumed_model_tools
            ):
                if skill_name:
                    self._attribute_skill_to_turn_history(
                        skill_name, None, None
                    )
                return _AWAITABLE_NONE
            if tool_name not in self._pending_tools:
                self._pending_tools.append(tool_name)
            if skill_name and skill_name not in self._pending_skills:
                self._pending_skills.append(skill_name)
        return _AWAITABLE_NONE

    def after_model_callback(
        self, callback_context: Any = None, llm_response: Any = None, **kwargs
    ) -> Any:
        """ADK lifecycle hook invoked after LLM response is received."""
        if (
            llm_response is None
            or getattr(llm_response, "partial", False) is True
        ):
            return _AWAITABLE_NONE

        raw_sid, invocation_id, agent_name = self._extract_context_keys(
            callback_context
        )
        session_id, parent_inv_id = self._resolve_effective_session(
            raw_sid, invocation_id, agent_name
        )
        state = self._resolve_turn_state(invocation_id, agent_name)
        flow_type = self._extract_orchestration_flow(
            callback_context, self._agent_tool_children
        )
        if state is not None:
            if flow_type and not state.get("orchestration_flow"):
                state["orchestration_flow"] = flow_type
            if not state.get("parent_session_id"):
                state["parent_session_id"] = session_id
            if parent_inv_id and not state.get("parent_invocation_id"):
                state["parent_invocation_id"] = parent_inv_id

        latency_ms = None
        agent_override_t = (
            self._agent_start_times.pop(agent_name, None)
            if agent_name
            else None
        )
        if state is not None and state.get("start_time") is not None:
            effective_start = (
                min(state["start_time"], agent_override_t)
                if agent_override_t is not None
                else state["start_time"]
            )
            latency_ms = self._elapsed_ms(effective_start)
            state["start_time"] = None
        elif agent_override_t is not None:
            latency_ms = self._elapsed_ms(agent_override_t)
        elif self._turn_start_time is not None:
            latency_ms = self._elapsed_ms(self._turn_start_time)
            self._turn_start_time = None

        user_query = (
            (state.get("user_query") if state else None)
            or self._extract_user_query_from_context(callback_context)
            or self._last_user_query
        )
        if user_query:
            self._last_user_query = user_query
        rec = self.on_event(
            event=llm_response,
            session_id=session_id,
            user_query=user_query,
            latency_ms=latency_ms,
            invocation_id=invocation_id,
            sub_agent_name=agent_name,
            _from_after_model=True,
        )
        if rec is not None:
            sig_key = (session_id, invocation_id, agent_name)
            self._recent_model_signatures[sig_key] = (
                int(rec.get("input_tokens", 0)),
                int(rec.get("cached_tokens", 0)),
                int(rec.get("output_tokens", 0)),
                int(rec.get("thinking_tokens", 0)),
            )
            while len(self._recent_model_signatures) > _MAX_TRACKED_INVOCATIONS:
                self._recent_model_signatures.popitem(last=False)
        return _AWAITABLE_NONE

    def on_event_callback(
        self, *, invocation_context: Any = None, event: Any = None, **kwargs
    ) -> Any:
        """ADK Runner event hook."""
        if event is None or getattr(event, "partial", False) is True:
            return _AWAITABLE_NONE

        raw_sid, invocation_id, ctx_agent = self._extract_context_keys(
            invocation_context
        )
        sub_agent = getattr(event, "author", None)
        resolved_agent = (
            str(sub_agent) if sub_agent and sub_agent != "user" else ctx_agent
        )
        session_id, _ = self._resolve_effective_session(
            raw_sid, invocation_id, resolved_agent
        )
        state = self._resolve_turn_state(invocation_id, resolved_agent)
        user_query = (
            (state.get("user_query") if state else None)
            or self._extract_user_query_from_context(invocation_context)
            or self._last_user_query
        )
        if user_query:
            self._last_user_query = user_query

        self.on_event(
            event=event,
            session_id=session_id,
            user_query=user_query,
            invocation_id=invocation_id,
            sub_agent_name=resolved_agent,
            _from_on_event_callback=True,
        )
        return _AWAITABLE_NONE

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        """Wall-clock milliseconds since `start`, rounded up so a measured call is never reported as 0 ms.

        A latency of 0 is reserved for "not captured", which `adk-tokenomics assert --max-latency-ms` treats as
        missing telemetry (fail-closed) rather than as an instantaneous call.
        """
        return max(1, math.ceil((time.time() - start) * 1000.0))

    @staticmethod
    def _get_usage_field(usage: Any, *field_names: str) -> int:
        for name in field_names:
            val = (
                usage.get(name)
                if isinstance(usage, dict)
                else getattr(usage, name, None)
            )
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError, OverflowError):
                    pass
        return 0

    def _mark_usage_seen(self, usage: Any) -> bool:
        """Returns True if this exact usage object was already recorded; otherwise tracks it in a bounded LRU."""
        usage_id = id(usage)
        if self._seen_usage_ids.get(usage_id) is usage:
            self._seen_usage_ids.move_to_end(usage_id)
            return True
        self._seen_usage_ids[usage_id] = usage
        self._seen_usage_ids.move_to_end(usage_id)
        while len(self._seen_usage_ids) > _MAX_SEEN_USAGE_IDS:
            self._seen_usage_ids.popitem(last=False)
        return False

    def on_event(
        self,
        event: Any,
        session_id: str = "default_session",
        user_query: str = "",
        latency_ms: int | None = None,
        invocation_id: str | None = None,
        sub_agent_name: str | None = None,
        _from_after_model: bool = False,
        _from_on_event_callback: bool = False,
    ) -> dict[str, Any] | None:
        """Extracts token metrics from ADK event or LlmResponse object (1P GenAI, 3P LiteLLM/Claude/OpenAI, or Open-Weight)."""
        if event is None or getattr(event, "partial", False) is True:
            return None
        usage = getattr(event, "usage_metadata", None) or getattr(
            event, "usage", None
        )
        if not usage:
            return None
        if self._mark_usage_seen(usage):
            return None

        # Extract token attributes across Google GenAI, camelCase adapters, Anthropic Claude, OpenAI, and LiteLLM schemas
        prompt_tokens = max(
            0,
            self._get_usage_field(
                usage,
                "prompt_token_count",
                "promptTokens",
                "prompt_tokens",
                "input_tokens",
                "inputTokens",
            ),
        )
        tool_use_prompt_tokens = max(
            0,
            self._get_usage_field(
                usage, "tool_use_prompt_token_count", "toolUsePromptTokenCount"
            ),
        )
        prompt_tokens += tool_use_prompt_tokens
        cached_tokens = max(
            0,
            self._get_usage_field(
                usage,
                "cached_content_token_count",
                "cachedContentTokenCount",
                "cacheReadTokens",
                "cached_tokens",
                "cache_read_input_tokens",
            ),
        )
        candidates_tokens = max(
            0,
            self._get_usage_field(
                usage,
                "candidates_token_count",
                "candidatesTokenCount",
                "completion_tokens",
                "completionTokens",
                "output_tokens",
                "outputTokens",
            ),
        )
        thinking_tokens = max(
            0,
            self._get_usage_field(
                usage,
                "thoughts_token_count",
                "thoughtsTokenCount",
                "thinkingOutputTokens",
                "thinking_tokens",
                "reasoning_tokens",
                "reasoningTokens",
            ),
        )

        # Fresh input is prompt minus cached (unless input_tokens was already reported net of cache_read_input_tokens)
        if (
            self._get_usage_field(
                usage, "prompt_token_count", "promptTokens", "prompt_tokens"
            )
            == 0
            and self._get_usage_field(usage, "input_tokens", "inputTokens") > 0
            and self._get_usage_field(
                usage, "cache_read_input_tokens", "cacheReadTokens"
            )
            > 0
        ):
            fresh_input = prompt_tokens
        else:
            if prompt_tokens > 0 and cached_tokens > prompt_tokens:
                cached_tokens = prompt_tokens
            elif prompt_tokens == 0 and cached_tokens > 0:
                raw_prompt_present = any(
                    (
                        usage.get(k)
                        if isinstance(usage, dict)
                        else getattr(usage, k, None)
                    )
                    is not None
                    for k in (
                        "prompt_token_count",
                        "promptTokens",
                        "prompt_tokens",
                    )
                )
                if raw_prompt_present:
                    cached_tokens = 0
            fresh_input = max(0, prompt_tokens - cached_tokens)

        resolved_inv_id = invocation_id or getattr(event, "invocation_id", None)
        resolved_sub_agent = sub_agent_name or getattr(event, "author", None)
        if resolved_sub_agent == "user":
            resolved_sub_agent = None

        # Guard against duplicate recording when ADK invokes after_model_callback followed by on_event_callback
        # with a reconstructed Event copy of the same LlmResponse.
        if _from_on_event_callback:
            sig_key = (
                session_id,
                str(resolved_inv_id) if resolved_inv_id else None,
                str(resolved_sub_agent) if resolved_sub_agent else None,
            )
            expected_sig = (
                fresh_input,
                cached_tokens,
                candidates_tokens,
                thinking_tokens,
            )
            if self._recent_model_signatures.get(sig_key) == expected_sig:
                self._recent_model_signatures.pop(sig_key, None)
                return None

        state = self._resolve_turn_state(
            str(resolved_inv_id) if resolved_inv_id else None,
            str(resolved_sub_agent) if resolved_sub_agent else None,
        )
        state_model = state.get("request_model") if state else None
        model_name = (
            getattr(event, "model_version", None)
            or getattr(event, "model", None)
            or state_model
            or self._last_request_model
            or self.tracker.config.default_model
        )

        # Extract response text and function calls if available
        response_text = ""
        if state is not None and (
            state["pending_tools"] or state["pending_skills"]
        ):
            tools_called = list(state["pending_tools"])
            skills_active = list(state["pending_skills"])
            state["pending_tools"].clear()
            state["pending_skills"].clear()
        else:
            tools_called = list(self._pending_tools)
            skills_active = list(self._pending_skills)
            self._pending_tools.clear()
            self._pending_skills.clear()

        response_tools: list[str] = []
        tool_call_previews: list[str] = []

        def _collect_part_tools(parts_seq: Any) -> None:
            for p in parts_seq:
                fc = getattr(p, "function_call", None)
                if fc and getattr(fc, "name", None):
                    t_name = str(fc.name)
                    response_tools.append(t_name)
                    if t_name not in tools_called:
                        tools_called.append(t_name)
                    fc_args = getattr(fc, "args", None)
                    if isinstance(fc_args, dict) and fc_args:
                        if (
                            "skill" in t_name.lower()
                            and "unload" not in t_name.lower()
                        ):
                            sk = (
                                fc_args.get("skill_name")
                                or fc_args.get("name")
                                or fc_args.get("skill")
                            )
                            if sk and str(sk) not in skills_active:
                                skills_active.append(str(sk))
                        arg_pairs = []
                        for k, v in list(fc_args.items())[:3]:
                            v_str = str(v)
                            if len(v_str) > 36:
                                v_str = v_str[:33] + "..."
                            arg_pairs.append(f"{k}={v_str!r}")
                        tool_call_previews.append(
                            f"{t_name}({', '.join(arg_pairs)})"
                        )
                    else:
                        tool_call_previews.append(t_name)

        candidates = getattr(event, "candidates", None)
        if candidates and len(candidates) > 0:
            content = getattr(candidates[0], "content", None)
            if content and hasattr(content, "parts") and content.parts:
                parts_text = [
                    getattr(p, "text", "")
                    for p in content.parts
                    if hasattr(p, "text") and getattr(p, "text", None)
                ]
                response_text = " ".join(filter(None, parts_text))
                _collect_part_tools(content.parts)

        content_obj = getattr(event, "content", None)
        if (
            not response_text
            and content_obj
            and getattr(content_obj, "parts", None)
        ):
            parts_text = [
                getattr(p, "text", "")
                for p in content_obj.parts
                if hasattr(p, "text") and getattr(p, "text", None)
            ]
            response_text = " ".join(filter(None, parts_text))
            if not response_tools:
                _collect_part_tools(content_obj.parts)

        if response_tools:
            if state is not None:
                state["last_model_tools"] = list(response_tools)
                state["consumed_model_tools"] = []
                state["awaiting_post_model_tools"] = True
            else:
                self._last_model_tools = list(response_tools)
                self._consumed_model_tools = []
                self._awaiting_post_model_tools = True

        if not response_text and tools_called:
            preview_items = (
                tool_call_previews if tool_call_previews else tools_called
            )
            response_text = f"🛠️ Tool call: {', '.join(preview_items)}"

        state_query = state.get("user_query") if state else None
        effective_query = user_query or state_query or self._last_user_query
        if resolved_sub_agent and not effective_query.startswith(
            f"[{resolved_sub_agent}]"
        ):
            effective_query = (
                f"[{resolved_sub_agent}] {effective_query}".strip()
            )

        raw_meta: dict[str, Any] = {
            "usage": str(usage),
            "sub_agent": str(resolved_sub_agent or self.app_name),
        }
        if state is not None and state.get("orchestration_flow"):
            raw_meta["orchestration_flow"] = str(state["orchestration_flow"])
        elif (
            resolved_sub_agent
            and str(resolved_sub_agent) in self._agent_tool_children
        ):
            raw_meta["orchestration_flow"] = "AgentTool"

        effective_session_id = (
            state.get("parent_session_id") if state else None
        ) or session_id
        effective_inv_id = (
            (state.get("parent_invocation_id") if state else None)
            or invocation_id
            or getattr(event, "invocation_id", None)
        )

        record = self.tracker.record_turn(
            session_id=effective_session_id,
            app_name=self.app_name,
            model_name=str(model_name),
            user_query=effective_query,
            agent_response=response_text[:1000],
            input_tokens=fresh_input,
            cached_tokens=cached_tokens,
            output_tokens=candidates_tokens,
            thinking_tokens=thinking_tokens,
            tools_called=tools_called,
            skills_active=skills_active,
            latency_ms=latency_ms,
            invocation_id=effective_inv_id,
            raw_json=raw_meta,
        )
        if self.auto_export_path:
            try:
                self._export_and_rotate_session(effective_session_id)
            except Exception:
                pass
        return record

    @staticmethod
    def _safe_session_filename(sid: str) -> str:
        safe = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in str(sid)
        )
        return f"{safe}.json"

    @staticmethod
    def _read_trace_file_payload(
        file_path: str,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Reads (session_id, turns) from an existing JSON trace file."""
        if not file_path or not os.path.exists(file_path):
            return None, []
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                turns_raw = data.get("turns")
                turns = (
                    [t for t in turns_raw if isinstance(t, dict)]
                    if isinstance(turns_raw, list)
                    else []
                )
                sid = data.get("session_id")
                if not sid and turns:
                    sid = turns[0].get("session_id")
                return (str(sid) if sid else None), turns
        except Exception:
            pass
        return None, []

    @staticmethod
    def _file_stat_sig(file_path: str) -> tuple[int, int, int] | None:
        """Returns (st_ino, st_mtime_ns, st_size) for fast cache validation across plugin instances."""
        try:
            st = os.stat(file_path)
            return (int(st.st_ino), int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

    @classmethod
    def _read_trace_file_session_id(cls, file_path: str) -> str | None:
        """Reads the session_id recorded inside an existing JSON trace file."""
        sid, _ = cls._read_trace_file_payload(file_path)
        return sid

    def _hydrate_resumed_session_if_needed(self, session_id: str) -> None:
        """Merges prior turns from <session_id>.json or auto_export_path when resuming a session across plugin/process instances."""
        if (
            not self.auto_export_path
            or not session_id
            or session_id == "default_session"
        ):
            return
        if session_id in self._hydrated_sessions:
            return
        self._hydrated_sessions.add(session_id)

        trace_dir = (
            os.path.dirname(os.path.abspath(self.auto_export_path)) or "."
        )
        archive_path = os.path.join(
            trace_dir, self._safe_session_filename(session_id)
        )
        prior_turns: list[dict[str, Any]] = []

        if os.path.exists(archive_path):
            arch_sid, arch_turns = self._read_trace_file_payload(archive_path)
            if arch_turns and (arch_sid in (None, session_id)):
                prior_turns.extend(
                    t
                    for t in arch_turns
                    if t.get("session_id") in (None, session_id)
                )

        if os.path.exists(self.auto_export_path) and os.path.abspath(
            self.auto_export_path
        ) != os.path.abspath(archive_path):
            live_sid, live_turns = self._read_trace_file_payload(
                self.auto_export_path
            )
            if live_turns and live_sid == session_id:
                prior_turns.extend(
                    t
                    for t in live_turns
                    if t.get("session_id") in (None, session_id)
                )

        if prior_turns:
            self.tracker.merge_historical_turns(prior_turns)

    def _export_and_rotate_session(self, session_id: str) -> None:
        """Writes the active session to auto_export_path, rotating previous sessions and hydrating resumed sessions."""
        if not self.auto_export_path:
            return
        abs_export = os.path.abspath(self.auto_export_path)
        was_displaced = False
        if os.path.exists(abs_export):
            current_stat = self._file_stat_sig(abs_export)
            if (
                self._last_exported_file_session_id is not None
                and self._last_exported_file_path == abs_export
                and self._last_exported_file_stat is not None
                and current_stat == self._last_exported_file_stat
            ):
                existing_sid: str | None = self._last_exported_file_session_id
            else:
                if (
                    self._last_exported_file_stat is not None
                    and current_stat != self._last_exported_file_stat
                ):
                    was_displaced = True
                existing_sid = self._read_trace_file_session_id(abs_export)
            if existing_sid and existing_sid not in (
                "default_session",
                session_id,
            ):
                self.finalize_session(session_id=existing_sid)
        else:
            if self._last_exported_file_stat is not None:
                was_displaced = True
            self._last_exported_file_session_id = None
            self._last_exported_file_path = None
            self._last_exported_file_stat = None

        if was_displaced and session_id and session_id != "default_session":
            self._hydrated_sessions.discard(session_id)
        self._hydrate_resumed_session_if_needed(session_id)
        self._active_export_session_id = session_id
        if session_id and session_id != "default_session":
            self._unfinalized_sessions.add(session_id)
        scoped_sid = (
            session_id
            if (session_id and session_id != "default_session")
            else None
        )
        self.tracker.export_traces(self.auto_export_path, session_id=scoped_sid)
        self._last_exported_file_session_id = session_id
        self._last_exported_file_path = abs_export
        self._last_exported_file_stat = self._file_stat_sig(abs_export)

    def _merge_turns_into_archive(
        self,
        trace_dir: str,
        sid: str,
        extra_turns: list[dict[str, Any]],
        remove_live_path: str | None = None,
        live_on_disk_count: int | None = None,
    ) -> str | None:
        """Atomically merges `extra_turns` (filtered to `sid`) into `<sid>.json`, optionally removing `remove_live_path`."""
        if not sid or sid == "default_session":
            return None
        target_path = os.path.join(
            trace_dir, self._safe_session_filename(str(sid))
        )
        abs_target = os.path.abspath(target_path)
        abs_remove = (
            os.path.abspath(remove_live_path) if remove_live_path else None
        )

        _, arch_turns = (
            self._read_trace_file_payload(abs_target)
            if os.path.exists(abs_target)
            else (None, [])
        )
        valid_arch = [
            t
            for t in arch_turns
            if isinstance(t, dict) and t.get("session_id") in (None, sid)
        ]
        valid_extra = [
            t
            for t in extra_turns
            if isinstance(t, dict) and t.get("session_id") in (None, sid)
        ]

        merged: list[dict[str, Any]] = []
        seen_keys = set()
        for t in valid_arch + valid_extra:
            k = self.tracker._turn_dedup_key(t)
            if k not in seen_keys:
                seen_keys.add(k)
                merged.append(t)

        if len(merged) > 1 and all(
            self.tracker._parse_iso_epoch(t.get("timestamp")) is not None
            for t in merged
        ):
            merged = [
                item[1]
                for item in sorted(
                    enumerate(merged),
                    key=lambda item: (
                        self.tracker._parse_iso_epoch(item[1].get("timestamp"))
                        or 0.0,
                        item[0],
                    ),
                )
            ]

        if (
            abs_remove
            and abs_remove != abs_target
            and os.path.exists(abs_remove)
            and not os.path.exists(abs_target)
            and not valid_arch
            and live_on_disk_count is not None
            and len(merged) == live_on_disk_count
        ):
            os.replace(abs_remove, abs_target)
            return target_path

        if not merged and not (abs_remove and os.path.exists(abs_remove)):
            return target_path if os.path.exists(abs_target) else None

        payload = {
            "summary": self.tracker._summarize_turns(merged),
            "turns": merged,
            "session_id": str(sid),
        }
        serialized = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=trace_dir,
                prefix=".tmp_fin_",
                suffix=".json",
                delete=False,
            ) as tf:
                tmp_path = tf.name
                tf.write(serialized)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, abs_target)
            tmp_path = None
            if (
                abs_remove
                and abs_remove != abs_target
                and os.path.exists(abs_remove)
            ):
                try:
                    os.remove(abs_remove)
                except OSError:
                    pass
            return target_path
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _atexit_finalize_session(self) -> None:
        """Finalizes non-default live session traces on process exit for any auto_export_path that recorded turns."""
        if not self.auto_export_path:
            return
        pending = [
            sid
            for sid in self._unfinalized_sessions
            if sid and sid != "default_session"
        ]
        if (
            self._active_export_session_id
            and self._active_export_session_id != "default_session"
            and self._active_export_session_id not in pending
        ):
            pending.append(self._active_export_session_id)
        for sid in pending:
            self.finalize_session(session_id=sid)
        if (
            self._active_export_session_id in pending
            or self._active_export_session_id == "default_session"
        ):
            self._active_export_session_id = None

    async def close(self) -> None:
        """ADK plugin shutdown hook: finalizes non-default session archives (idempotent with atexit) and flushes sink buffers."""
        self._atexit_finalize_session()
        flush_fn = getattr(self.tracker.sink, "flush", None)
        if callable(flush_fn):
            res = flush_fn()
            if inspect.isawaitable(res):
                await res

    def finalize_session(self, session_id: str | None = None) -> str | None:
        """Archives the live session trace file to <session_id>.json upon session termination, preserving prior archived turns."""
        if not self.auto_export_path:
            self._last_exported_file_session_id = None
            self._last_exported_file_path = None
            self._last_exported_file_stat = None
            return None
        try:
            abs_live = os.path.abspath(self.auto_export_path)
            trace_dir = os.path.dirname(abs_live) or "."
            if not os.path.isdir(trace_dir):
                self._last_exported_file_session_id = None
                self._last_exported_file_path = None
                self._last_exported_file_stat = None
                return None

            live_exists = os.path.exists(abs_live)
            live_sid: str | None = None
            live_turns: list[dict[str, Any]] = []
            if live_exists:
                live_sid, live_turns = self._read_trace_file_payload(abs_live)
                if not live_sid:
                    current_stat = self._file_stat_sig(abs_live)
                    if (
                        self._last_exported_file_session_id is not None
                        and self._last_exported_file_path == abs_live
                        and self._last_exported_file_stat is not None
                        and current_stat == self._last_exported_file_stat
                    ):
                        live_sid = self._last_exported_file_session_id

            requested_sid = (
                session_id or live_sid or self._active_export_session_id
            )
            result_path: str | None = None

            # 1. If the live file exists and belongs to a valid session Y, archive Y to Y.json (never to X != Y)
            if live_exists and live_sid and live_sid != "default_session":
                with self.tracker._lock:
                    own_for_live = (
                        self.tracker._get_merged_turns_for_export_locked(
                            session_id=live_sid
                        )
                    )
                combined_live = list(live_turns) + list(own_for_live)
                all_belong_to_live = all(
                    isinstance(t, dict)
                    and t.get("session_id") in (None, live_sid)
                    for t in live_turns
                )
                unique_live_keys = (
                    {
                        self.tracker._turn_dedup_key(t)
                        for t in live_turns
                        if isinstance(t, dict)
                    }
                    if all_belong_to_live
                    else set()
                )
                live_disk_count = (
                    len(live_turns)
                    if (
                        all_belong_to_live
                        and len(unique_live_keys) == len(live_turns)
                    )
                    else None
                )
                live_arch_path = self._merge_turns_into_archive(
                    trace_dir=trace_dir,
                    sid=live_sid,
                    extra_turns=combined_live,
                    remove_live_path=abs_live,
                    live_on_disk_count=live_disk_count,
                )
                self._unfinalized_sessions.discard(live_sid)
                if self._active_export_session_id == live_sid:
                    self._active_export_session_id = None
                if requested_sid == live_sid:
                    result_path = live_arch_path

            # 2. Independently ensure requested_sid flushes this plugin's own in-memory turns to <requested_sid>.json
            if (
                requested_sid
                and requested_sid != "default_session"
                and result_path is None
            ):
                if (live_exists and live_sid is None) or (
                    requested_sid in self._unfinalized_sessions
                ):
                    with self.tracker._lock:
                        own_turns = (
                            self.tracker._get_merged_turns_for_export_locked(
                                session_id=requested_sid
                            )
                        )
                    if own_turns or (live_exists and live_sid is None):
                        result_path = self._merge_turns_into_archive(
                            trace_dir=trace_dir,
                            sid=requested_sid,
                            extra_turns=own_turns,
                            remove_live_path=abs_live
                            if (live_exists and live_sid is None)
                            else None,
                            live_on_disk_count=None,
                        )
                    self._unfinalized_sessions.discard(requested_sid)
                    if self._active_export_session_id == requested_sid:
                        self._active_export_session_id = None

            self._last_exported_file_session_id = None
            self._last_exported_file_path = None
            self._last_exported_file_stat = None
            return result_path
        except Exception:
            return None
