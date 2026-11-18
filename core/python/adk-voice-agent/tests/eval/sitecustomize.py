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

"""Startup hook for voice eval servers when `VOICE_EVAL_FAKES=1` is set.

Wires `tests/fakes.py` and `tests/fake_integrations.py` into the server process so `app/`
stays free of test branches while voice evals run deterministically.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
ROOT_DIR = EVAL_DIR.parents[1]
OPT_IN = "VOICE_EVAL_FAKES"


def _install_eval_fakes() -> None:
    run_dir = os.environ.get("EVAL_RUN_DIR")
    if run_dir:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="server-", dir=run_dir or None))
    # Set before importing `app`, which registers memory tools at import time when Agent Engine is configured.
    os.environ.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ID", "eval-fake-engine")
    os.environ.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-central1")
    state_dir = root / "state"
    state_dir.mkdir()
    os.environ["VOICE_AGENT_STATE_DIR"] = str(state_dir)
    os.environ.setdefault("VOICE_AGENT_SKIP_STARTUP_WARMUP", "1")
    os.environ.setdefault("MAX_RUNNING_THREADS_PER_USER", "20")
    # `agents-cli eval generate` opens sessions as `eval-cli-user`.
    os.environ.setdefault("VOICE_AGENT_DEV_USER_ID", "eval-cli-user")
    os.environ.setdefault(
        "VOICE_AGENT_WORKSPACES_LOCAL_YAML",
        str(EVAL_DIR / "voice" / "workspaces.yaml"),
    )
    import yaml

    import app.tools.grounding_tools as grounding_mod
    from app.app_utils.http_client import set_default_http_transport
    from app.auth import record_provider_state
    from app.auth.providers import all_providers
    from app.threads import driver
    from app.workers.environment import LocalEnvironment
    from app.workers.factory import set_environment
    from app.workers.harnesses.antigravity import AntigravityHarness
    from app.workers.harnesses.claude import ClaudeCodeHarness
    from app.workers.harnesses.registry import (
        HarnessRegistry,
        set_harness_registry,
    )
    from tests.eval.history import current
    from tests.fake_integrations import integrations_transport
    from tests.fakes import FakeAgent, app_client_env

    for key, value in app_client_env().items():
        os.environ.setdefault(key, value)
    disconnected = set(current().disconnected)
    connected = [
        p for p in all_providers().values() if p.name not in disconnected
    ]
    for p in connected:
        os.environ.setdefault(
            p.access_token_env, f"eval-{p.access_token_env.lower()}"
        )

    driver.POLL_INTERVAL_S = 0.05
    driver.PROGRESS_MIN_INTERVAL_S = 0.0
    driver.RETRY_DELAY_S = 0.0

    set_default_http_transport(
        integrations_transport(workspace=_persona_workspace())
    )
    for p in connected:
        record_provider_state(
            p.name, f"{p.display_name} Eval", expires_in=86400
        )

    async def _eval_specialist(
        agent: object, prompt: str
    ) -> grounding_mod.Grounded:
        if "maps" in getattr(agent, "name", ""):
            return grounding_mod.Grounded(
                "Medici Roasting on Congress Avenue "
                "is open now with a 4.7 star rating and quiet upstairs Wi-Fi seating. "
                "Houndstooth Coffee, 0.6 mi away, is rated 4.6 and open until 8 PM.",
                [
                    {
                        "title": "Medici Roasting",
                        "url": "https://maps.google.com/?cid=1001",
                        "summary": "Quiet upstairs seating with fast Wi-Fi.",
                    },
                    {
                        "title": "Houndstooth Coffee",
                        "url": "https://maps.google.com/?cid=1002",
                        "summary": "Pour-over bar with long communal tables.",
                    },
                ],
            )
        return grounding_mod.Grounded(
            "Python 3.13 introduces an experimental free-threaded mode that disables "
            "the global interpreter lock, plus an experimental JIT compiler.",
            [
                {
                    "title": "What's New In Python 3.13",
                    "url": "https://docs.python.org/3/whatsnew/3.13.html",
                    "domain": "docs.python.org",
                },
                {
                    "title": "Python 3.13: Free threading and a JIT compiler",
                    "url": "https://realpython.com/python313-new-features/",
                    "domain": "realpython.com",
                },
            ],
        )

    grounding_mod._run_specialist = _eval_specialist

    set_harness_registry(
        HarnessRegistry([ClaudeCodeHarness(), AntigravityHarness()])
    )

    agent = FakeAgent(root / "agent")
    agent.scenarios(
        yaml.safe_load((EVAL_DIR / "voice" / "scenarios.yaml").read_text())
    )
    os.environ.update(agent.env)
    set_environment(LocalEnvironment(root / "workspace"))
    _patch_adk_agent_info()
    _install_history()
    _record_follow_ups()


def _record_follow_ups() -> None:
    """Mirror async `live_request_queue` tool follow-ups onto the session event stream so `/run_live` traces include them."""
    import asyncio

    from google.adk.events.event import Event
    from google.genai import types

    from app.agent import root_agent
    from app.voice.thread_updates import BACKGROUND_UPDATE

    hydrate = root_agent.before_agent_callback
    if not callable(hydrate):
        raise RuntimeError(
            "expected one before_agent_callback on the voice agent"
        )
    pending: set[asyncio.Task] = set()

    def _is_placeholder(response: types.FunctionResponse) -> bool:
        status = (response.response or {}).get("status")
        return isinstance(status, str) and "results are pending" in status

    def recorded(content: types.Content) -> types.Content | None:
        parts = [
            types.Part(function_response=p.function_response)
            if p.function_response
            else types.Part(
                function_response=types.FunctionResponse(
                    name="background_thread_update", response={"result": p.text}
                )
            )
            for p in content.parts or []
            if (
                p.function_response and not _is_placeholder(p.function_response)
            )
            or (p.text or "").startswith(BACKGROUND_UPDATE)
        ]
        return types.Content(role="user", parts=parts) if parts else None

    async def hydrate_and_record(callback_context: Any) -> None:
        await hydrate(callback_context)  # type: ignore[misc]
        ctx = getattr(callback_context, "_invocation_context", None)
        queue = getattr(ctx, "live_request_queue", None)
        if (
            ctx is None
            or queue is None
            or getattr(queue, "_eval_recorded", False)
        ):
            return
        send = queue.send_content

        def send_and_record(
            content: types.Content, *args: Any, **kw: Any
        ) -> None:
            send(content, *args, **kw)
            if (copy := recorded(content)) is None:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            event = Event(
                author=ctx.agent.name,
                invocation_id=ctx.invocation_id,
                content=copy,
            )
            thread = loop.create_task(ctx._enqueue_event(event))
            pending.add(thread)
            thread.add_done_callback(pending.discard)

        queue.send_content = send_and_record
        queue._eval_recorded = True

    root_agent.before_agent_callback = hydrate_and_record


def _patch_adk_agent_info() -> None:
    """Allow ADK's `/apps/{app_name}/app-info` endpoint to serialize callable instructions."""
    import inspect

    import google.adk.cli.api_server as api_server_mod
    import google.adk.utils.agent_info as agent_info_mod

    async def _get_agents_dict(
        agent: object,
    ) -> dict[str, agent_info_mod.AgentInfo]:
        agents_dict: dict[str, agent_info_mod.AgentInfo] = {}

        async def _traverse(current_agent: Any) -> None:
            if current_agent.name in agents_dict:
                return
            sub_agent_names: list[str] = []
            for sub_agent in getattr(current_agent, "sub_agents", []) or []:
                if isinstance(sub_agent, agent_info_mod.LlmAgent):
                    await _traverse(sub_agent)
                    sub_agent_names.append(sub_agent.name)
            raw_inst = current_agent.instruction
            if callable(raw_inst):
                res = raw_inst(None)
                inst_str = await res if inspect.isawaitable(res) else str(res)
            else:
                inst_str = str(raw_inst or "")
            agents_dict[current_agent.name] = agent_info_mod.AgentInfo(
                name=current_agent.name,
                description=current_agent.description or "",
                instruction=inst_str,
                tools=await agent_info_mod.get_tools_info(
                    list(current_agent.tools or [])
                ),
                sub_agents=sub_agent_names,
            )

        await _traverse(agent)
        return agents_dict

    agent_info_mod.get_agents_dict = _get_agents_dict  # type: ignore[assignment]
    api_server_mod.get_agents_dict = _get_agents_dict  # type: ignore[assignment]


def _persona_workspace() -> Any:
    """The persona's Calendar, Gmail, and Drive (`persona.yaml` `apps`), as System 2 sees them."""
    from datetime import UTC, datetime, time, timedelta

    from tests.eval.system2_agent.snapshot import load_persona
    from tests.fake_integrations import Workspace

    persona = load_persona("alex")
    apps = persona.data.get("apps") or {}

    def events(now: datetime) -> list[dict[str, Any]]:
        def at(e: dict[str, Any], key: str) -> str:
            day = persona.today + timedelta(days=e.get("day", 0))
            clock = time.fromisoformat(e[key])
            return datetime.combine(day, clock, now.tzinfo).isoformat()

        return [
            {
                "id": e["id"],
                "summary": e["summary"],
                "start": {"dateTime": at(e, "start")},
                "end": {"dateTime": at(e, "end")},
                "location": e.get("location", ""),
                "description": e.get("description", ""),
                "attendees": [{"email": a} for a in e.get("attendees") or []],
            }
            for e in apps.get("calendar") or []
        ]

    messages = {
        m["id"]: (
            m["from"],
            m["subject"],
            m["body"],
            m.get("hours_ago", 0) * 60,
            list(m.get("labels") or []),
        )
        for m in apps.get("gmail") or []
    }
    files = [
        {
            "id": f["id"],
            "name": f["title"],
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": (
                datetime.now(UTC)
                - timedelta(days=f.get("modified_days_ago", 0))
            ).isoformat(),
            "owners": [
                {"emailAddress": f["owner"], "me": f["owner"] == apps.get("me")}
            ],
        }
        for f in apps.get("drive") or []
    ]
    return Workspace(events=events, messages=messages, files=files)


def _install_history() -> None:
    from app.auth.identity import dev_user_id
    from app.memory import episodes
    from app.memory.store import set_memory_store
    from app.voice.focus import set_focus
    from tests.eval.history import current, seconds_ago
    from tests.fakes import FakeEpisodeStore, FakeMemoryStore

    history, user = current(), dev_user_id()
    if history.focus:
        set_focus(
            view=history.focus.get("view", "home"),
            project=history.focus.get("project", ""),
            thread_id=history.focus.get("thread_id", ""),
            user_id=user,
        )
    profile = FakeMemoryStore()
    if history.profile:
        profile.set_profile(user, **history.profile)
    set_memory_store(profile)
    remembered = FakeEpisodeStore()
    for m in history.memories:
        ref = m.get("thread_id") or m.get("session_id") or m["fact"]
        memory_id = episodes.memory_id(m["kind"], user, ref)
        metadata: dict[str, Any] = {
            k: m[k] for k in ("thread_id", "session_id") if k in m
        }
        remembered.memories.setdefault((user, m["kind"]), {})[memory_id] = (
            episodes.Episode(
                id=memory_id,
                kind=m["kind"],  # type: ignore[arg-type]
                fact=m["fact"],
                created_at=seconds_ago(m.get("ago")),
                metadata=metadata,
            )
        )
    episodes.set_episode_store(remembered)

    async def recap(turns: list[tuple[str, str]]) -> str:
        return " ".join(text for _, text in turns)[:300]

    episodes.set_summarizer(recap)


try:  # Guard for tools that share PYTHONPATH without app dependencies.
    import google.adk  # noqa: F401
except ImportError:
    pass
else:
    if os.environ.get(OPT_IN) == "1":
        if str(ROOT_DIR) not in sys.path:
            sys.path.insert(0, str(ROOT_DIR))
        _install_eval_fakes()
