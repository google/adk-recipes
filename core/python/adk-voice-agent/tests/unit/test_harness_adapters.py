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

"""Harness adapters: argv, stream parsing, settings, and the registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.workers.harnesses.activity import mcp_activity
from app.workers.harnesses.antigravity import AntigravityHarness
from app.workers.harnesses.base import (
    HarnessEvent,
    HarnessEventKind,
    HarnessResult,
    RunRequest,
)
from app.workers.harnesses.claude import ClaudeCodeHarness


def _claude_tool(name: str, **tool_input) -> dict:
    content = [{"type": "tool_use", "name": name, "input": tool_input}]
    return {"type": "assistant", "message": {"content": content}}


def test_claude_stream_parsing():
    harness = ClaudeCodeHarness()
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Looking around\nmore"}]
            },
        },
        _claude_tool("Edit", file_path="/w/app/auth.py"),
        _claude_tool("Bash", command="pytest -q"),
        {
            "type": "result",
            "subtype": "success",
            "result": "Done",
        },
        {"type": "result", "subtype": "error_max_turns", "result": ""},
    ]
    state: dict = {}
    events = [
        e for line in lines for e in harness.parse_line(json.dumps(line), state)
    ]
    assert events[:4] == [
        HarnessEvent.session("s-1"),
        HarnessEvent.progress("Looking around"),
        HarnessEvent.activity("Editing app/auth.py"),
        HarnessEvent.activity("Running pytest -q"),
    ]
    assert events[4].result == HarnessResult("Done")
    assert events[5].result == HarnessResult("error_max_turns", is_error=True)
    assert harness.parse_line("not json", state) == []
    assert harness.parse_line("[1]", state) == []


_THREAD_DIR = "/home/.voice-agent/threads/t"


def test_claude_argv():
    harness = ClaudeCodeHarness()
    req = RunRequest(
        "t",
        "r",
        "p",
        "sys",
        "/w",
        "sid",
        resume=False,
        thread_dir=_THREAD_DIR,
    )
    first_argv = harness.build_argv(req)
    assert (
        first_argv[first_argv.index("--permission-mode") + 1]
        == "bypassPermissions"
    )
    assert first_argv[first_argv.index("--session-id") + 1] == "sid"
    # The thread context and the policy come from per-thread files in the thread directory,
    # never the working directory, rebuilt every request.
    assert (
        first_argv[first_argv.index("--append-system-prompt-file") + 1]
        == f"{_THREAD_DIR}/context.md"
    )
    assert first_argv[first_argv.index("--system-prompt-snapshot") + 1] == "off"
    assert (
        first_argv[first_argv.index("--settings") + 1]
        == f"{_THREAD_DIR}/claude-settings.json"
    )
    assert "sys" not in first_argv
    req.resume = True
    exec_argv = harness.build_argv(req)
    assert (
        exec_argv[exec_argv.index("--permission-mode") + 1]
        == "bypassPermissions"
    )
    assert (
        exec_argv[exec_argv.index("--resume") + 1] == "sid"
        and "--json-schema" not in exec_argv
        and "--allowedTools" not in exec_argv
        and "--mcp-config" not in exec_argv
        and "--permission-prompt-tool" not in exec_argv
    )
    req.env = {
        "VOICE_AGENT_MCP_URL": "http://127.0.0.1:8000/api/v1/mcp",
        "VOICE_AGENT_MCP_TOKEN": "tok-123",
    }
    mcp_argv = harness.build_argv(req)
    assert mcp_argv[
        mcp_argv.index("--mcp-config") + 1
    ] == f"{_THREAD_DIR}/mcp.json" and "tok-123" not in " ".join(mcp_argv)
    assert not any(a.startswith("/w/") for a in mcp_argv)


_POLICY = {
    "deny": [
        "*metadata.google.internal*",
        "* | curl *",
        "env | *",
        "rm -rf /*",
        "gcloud auth print-access-token*",
        "curl *",
    ],
}


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch):
    from app.workers.policy import Policy

    loaded = Policy(permissions=_POLICY)
    for module in ("claude", "antigravity"):
        monkeypatch.setattr(
            f"app.workers.harnesses.{module}.load_policy", lambda: loaded
        )


@pytest.mark.asyncio
async def test_claude_thread_files_hold_the_context_policy_and_gateway(
    tmp_path: Path, policy
):
    from app.workers.environment import LocalEnvironment
    from app.workers.harnesses.base import write_thread_files

    clone = tmp_path / "clone"
    clone.mkdir()
    va = tmp_path / ".voice-agent" / "threads" / "t"
    req = RunRequest(
        "t",
        "r",
        "p",
        "You are working in /w.",
        str(clone),
        "sid",
        resume=False,
        thread_dir=str(va),
        env={
            "VOICE_AGENT_MCP_URL": "http://127.0.0.1:8000/api/v1/mcp",
            "VOICE_AGENT_MCP_TOKEN": "mcp-secret-token",
        },
    )
    await write_thread_files(
        LocalEnvironment(tmp_path),
        str(va),
        ClaudeCodeHarness().thread_files(req),
    )
    assert (va / "context.md").read_text() == "You are working in /w."
    # Skills live in the computer's ~/.agents/skills; nothing is copied per thread.
    assert sorted(f.name for f in va.iterdir()) == [
        "claude-settings.json",
        "context.md",
        "mcp.json",
    ]
    assert not any(clone.iterdir())
    assert json.loads((va / "mcp.json").read_text()) == {
        "mcpServers": {
            "voice_agent": {
                "type": "http",
                "url": "${VOICE_AGENT_MCP_URL}",
                "headers": {"Authorization": "Bearer ${VOICE_AGENT_MCP_TOKEN}"},
            }
        }
    }
    # Only deny rules: execution bypasses prompts, and deny rules hold in every mode.
    assert json.loads((va / "claude-settings.json").read_text()) == {
        "permissions": {"deny": [f"Bash({g})" for g in _POLICY["deny"]]}
    }
    written = "".join(f.read_text() for f in tmp_path.rglob("*") if f.is_file())
    assert "mcp-secret-token" not in written


@pytest.mark.asyncio
async def test_start_leaves_harness_config_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy
):
    from app.workers import runs
    from app.workers.environment import LocalEnvironment

    launched: dict = {}

    async def launch(env, **kwargs):
        launched.update(kwargs)
        return runs.RunHandle(run_id=kwargs["run_id"], run_dir=str(tmp_path))

    monkeypatch.setattr(runs, "launch", launch)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    clone = tmp_path / "clone"
    clone.mkdir()
    thread_dir = tmp_path / ".voice-agent" / "threads" / "t"
    req = RunRequest(
        "t",
        "r",
        "p",
        "sys",
        str(clone),
        "s",
        False,
        thread_dir=str(thread_dir),
    )
    await ClaudeCodeHarness().start(LocalEnvironment(tmp_path), req)
    assert "CLAUDE_CONFIG_DIR" not in launched["env_vars"]
    assert launched["env_vars"]["VOICE_AGENT_THREAD_CLONE"] == str(clone)
    assert launched["cwd"] == str(clone)
    assert (thread_dir / "context.md").read_text() == "sys"
    # No gateway URL and token in the run's environment: no MCP config to point at.
    assert not (thread_dir / "mcp.json").exists()
    # The project folder holds only the user's work.
    assert not any(clone.iterdir())


@pytest.mark.asyncio
async def test_thread_files_need_a_thread_dir(tmp_path: Path):
    from app.workers.environment import LocalEnvironment
    from app.workers.harnesses.base import write_thread_files

    with pytest.raises(ValueError, match="thread_dir"):
        await write_thread_files(
            LocalEnvironment(tmp_path), "", {"context.md": "sys"}
        )


def test_claude_subagents_and_the_opus_alias_use_the_main_model(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.app_utils.models import require_model

    for key in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    env = ClaudeCodeHarness().build_env({})
    assert (
        env["ANTHROPIC_MODEL"]
        == env["ANTHROPIC_DEFAULT_OPUS_MODEL"]
        == env["CLAUDE_CODE_SUBAGENT_MODEL"]
        == "claude-opus-5-5"
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-other")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "claude-sub")
    env = ClaudeCodeHarness().build_env({})
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-other"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sub"
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(RuntimeError, match=r"ANTHROPIC_MODEL.*\.env\.example"):
        require_model("ANTHROPIC_MODEL")


def test_antigravity_signs_in_with_application_default_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("AGY_ADC_AUTH", raising=False)
    assert AntigravityHarness().build_env({})["AGY_ADC_AUTH"] == "1"


def test_antigravity_argv():
    harness = AntigravityHarness()
    req = RunRequest("t", "r", "p", "sys", "/w", "c-1", resume=True)
    exec_argv = harness.build_argv(req)
    assert "--dangerously-skip-permissions" in exec_argv
    assert "--mode" not in exec_argv
    assert exec_argv[exec_argv.index("--conversation") + 1] == "c-1"
    # Each thread is its own agy project, which holds the deny rules.
    assert exec_argv[exec_argv.index("--project") + 1] == "voice-agent-t"
    # agy reads AGENTS.md only lazily in headless runs, so the context leads the prompt.
    assert exec_argv[exec_argv.index("-p") + 1] == "sys\n\np"


def test_antigravity_renders_the_deny_globs_it_can_express(
    caplog: pytest.LogCaptureFixture,
):
    import logging

    from app.workers.harnesses.antigravity import (
        _warned_deny_globs,
        deny_rules,
    )

    _warned_deny_globs.clear()
    with caplog.at_level(logging.WARNING):
        assert deny_rules(_POLICY) == [
            "command(rm -rf /)",
            "command(gcloud auth print-access-token)",
            "command(curl)",
        ]
        first_warnings = [
            r.message
            for r in caplog.records
            if "cannot express the shell deny glob" in r.message
        ]
        assert len(first_warnings) == 3
        caplog.clear()
        deny_rules(_POLICY)
        assert not [
            r.message
            for r in caplog.records
            if "cannot express the shell deny glob" in r.message
        ]


@pytest.mark.asyncio
async def test_antigravity_project_file_is_per_thread_in_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy
):
    from app.workers.environment import LocalEnvironment
    from app.workers.harnesses.base import write_thread_files

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    clone = tmp_path / "clone"
    clone.mkdir()
    thread_dir = tmp_path / ".voice-agent" / "threads" / "t-1"
    req = RunRequest(
        "t-1",
        "r",
        "p",
        "sys",
        str(clone),
        None,
        resume=False,
        thread_dir=str(thread_dir),
        env={
            "VOICE_AGENT_MCP_URL": "http://127.0.0.1:8000/api/v1/mcp",
            "VOICE_AGENT_MCP_TOKEN": "mcp-secret-token",
        },
    )
    env = LocalEnvironment(tmp_path)
    await write_thread_files(
        env, str(thread_dir), AntigravityHarness().thread_files(req)
    )
    project_file = (
        tmp_path / "home/.gemini/config/projects/voice-agent-t-1.json"
    )
    project = json.loads(project_file.read_text())
    assert project == {
        "id": "voice-agent-t-1",
        "name": "voice-agent-t-1",
        "projectResources": {"resources": [{"folderUri": f"file://{clone}"}]},
        "permissionGrants": {
            "permissionGrants": {
                "deny": [
                    "command(rm -rf /)",
                    "command(gcloud auth print-access-token)",
                    "command(curl)",
                ]
            }
        },
    }
    written = "".join(f.read_text() for f in tmp_path.rglob("*") if f.is_file())
    assert "mcp-secret-token" not in written
    # No per-thread global MCP config: the image's entrypoint writes the gateway entry once.
    assert not (tmp_path / "home/.gemini/config/mcp_config.json").exists()
    assert (thread_dir / "context.md").read_text() == "sys"
    assert not any(clone.iterdir())
    # The project file is the one thing outside the thread directory; cleanup removes it.
    other = project_file.with_name("voice-agent-t-2.json")
    other.write_text("{}")
    await AntigravityHarness().cleanup(env, "t-1")
    assert not project_file.exists() and other.exists()
    await AntigravityHarness().cleanup(env, "t-1")  # idempotent


def test_antigravity_runs_argon_at_high_effort(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTIGRAVITY_EFFORT", raising=False)
    req = RunRequest("t", "r", "p", "sys", "/w", None, resume=False)
    argv = AntigravityHarness().build_argv(req)
    assert argv[argv.index("--model") + 1] == "gemini-4-argon"
    assert argv[argv.index("--effort") + 1] == "medium"
    monkeypatch.setenv("ANTIGRAVITY_EFFORT", "high")
    argv = AntigravityHarness().build_argv(req)
    assert argv[argv.index("--effort") + 1] == "high"


def test_antigravity_print_timeout_follows_the_run_budget():
    from app.workers import runs

    req = RunRequest("t", "r", "p", "sys", "/w", None, resume=False)
    argv = AntigravityHarness().build_argv(req)
    budget = round(runs.default_max_runtime_s())
    assert argv[argv.index("--print-timeout") + 1] == f"{budget}s"


@pytest.mark.asyncio
async def test_a_run_ends_at_its_successful_result_line(
    monkeypatch: pytest.MonkeyPatch,
):
    """The harness process may linger after its result (agy stops its MCP clients);
    the run is over at the result line, while an error result waits for the exit."""
    from app.workers import runs
    from app.workers.harnesses.base import CliRun

    result = {
        "event": "result",
        "result": {"status": "SUCCESS", "response": "ok"},
    }
    error = {"event": "result", "result": {"status": "ERROR", "error": "quota"}}
    lines: list[str] = []

    async def fake_poll(env, handle, cursor):
        del env, handle, cursor
        return runs.RunPoll(lines=lines, bytes_read=1)

    monkeypatch.setattr(runs, "poll", fake_poll)
    harness = AntigravityHarness()
    run = CliRun(runs.RunHandle("r", "/runs/r"))

    lines[:] = [json.dumps(result)]
    assert (await harness.poll(None, run)).exit_code == 0  # type: ignore[arg-type]
    lines[:] = [json.dumps(error)]
    assert (await harness.poll(None, run)).exit_code is None  # type: ignore[arg-type]


def _agy_step(index: int, state: str, step_type: str, **fields) -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": index,
            "state": state,
            "step_type": step_type,
            **fields,
        },
    }


def _agy_tool(index: int, state: str, name: str, **params) -> dict:
    return _agy_step(
        index,
        state,
        "tool",
        tool_name=name,
        tool_info={"name": name, "parameters": params},
    )


def test_antigravity_stream_parsing():
    harness = AntigravityHarness()
    lines = [
        {"event": "init", "conversation_id": "c-1"},
        _agy_step(1, "ACTIVE", "agent_response", text_delta="Let me look"),
        _agy_step(
            1, "ACTIVE", "agent_response", text_delta=" at the tests.\n\nThen"
        ),
        _agy_step(1, "ACTIVE", "agent_response", text_delta=" more."),
        _agy_step(1, "DONE", "agent_response"),
        _agy_tool(2, "ACTIVE", "read_file", Path="/w/tests/test_greet.py"),
        _agy_tool(2, "DONE", "read_file", Path="/w/tests/test_greet.py"),
        _agy_tool(3, "ACTIVE", "run_command", CommandLine="pytest -q"),
        _agy_tool(4, "ACTIVE", "browser_subagent"),
        _agy_tool(5, "ACTIVE", "finish"),
        _agy_tool(
            8,
            "ACTIVE",
            "call_mcp_tool",
            ServerName="voice_agent",
            ToolName="gmail__get_thread",
            Arguments={"thread_id": "t1"},
        ),
        _agy_tool(9, "ACTIVE", "edit_file", Path="/w/app/greet.py"),
        _agy_step(
            7, "ACTIVE", "agent_response", text_delta="Short final answer"
        ),
        _agy_step(7, "DONE", "agent_response"),
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "Done",
                "conversation_id": "c-1",
            },
        },
        {"event": "result", "result": {"status": "ERROR", "error": "quota"}},
    ]
    state: dict = {}
    events = [
        e for line in lines for e in harness.parse_line(json.dumps(line), state)
    ]
    assert [e for e in events if e.kind is not HarnessEventKind.RESULT] == [
        HarnessEvent.session("c-1"),
        HarnessEvent.progress("Let me look at the tests."),
        HarnessEvent.activity("Reading tests/test_greet.py"),
        HarnessEvent.activity("Running pytest -q"),
        HarnessEvent.activity("Using browser_subagent"),
        HarnessEvent.activity("Checking your email"),
        HarnessEvent.activity("Editing app/greet.py"),
        HarnessEvent.progress("Short final answer"),
        HarnessEvent.session("c-1"),
    ]
    results = [e.result for e in events if e.kind is HarnessEventKind.RESULT]
    assert results == [
        HarnessResult("Done"),
        HarnessResult("quota", is_error=True),
    ]


def test_antigravity_interrupted_results():
    harness = AntigravityHarness()
    interrupted = "The stream was interrupted. Please continue the task."

    def result(**res) -> HarnessResult:
        (event,) = harness.parse_line(
            json.dumps({"event": "result", "result": res}), {}
        )
        assert event.result is not None
        return event.result

    cut_off = result(status="ERROR", error=interrupted, response="Halfway")
    assert cut_off.is_error and cut_off.retryable

    busy = result(
        status="ERROR", error="API error: Error 503, Status: UNAVAILABLE"
    )
    assert busy.is_error and busy.retryable
    assert not result(status="ERROR", error="invalid argument").retryable


def test_harness_registry_resolves_aliases_and_the_default(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.workers.harnesses.registry import HarnessRegistry

    monkeypatch.setenv("CODING_HARNESS", "agy")
    registry = HarnessRegistry()
    assert [h.name for h in registry.list_all()] == ["claude", "antigravity"]
    assert registry.default_harness.name == "antigravity"
    assert registry.get("Claude Code").name == "claude"
    with pytest.raises(ValueError, match="Unknown coding harness"):
        registry.get("nope")
    # Threads stored by a since-removed harness still read and serialize.
    assert registry.display_name("agy") == "Antigravity"
    assert registry.display_name("horizon") == "horizon"
    assert registry.ref("agy") == {
        "name": "antigravity",
        "display_name": "Antigravity",
        "icon": "antigravity",
    }
    assert registry.ref("horizon") == {
        "name": "horizon",
        "display_name": "horizon",
        "icon": "",
    }


def test_harnesses_label_native_search_the_same_way():
    from app.workers.harnesses.base import WEB_SEARCH_VERB

    claude = ClaudeCodeHarness().parse_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "WebSearch",
                            "input": {"query": "httpx"},
                        }
                    ]
                },
            }
        ),
        {},
    )
    agy = AntigravityHarness().parse_line(
        json.dumps(_agy_tool(1, "ACTIVE", "search_web", query="httpx")), {}
    )
    for events in (claude, agy):
        assert events[0].message == f"{WEB_SEARCH_VERB} httpx"


@pytest.mark.parametrize(
    ("name", "line"),
    [
        (
            "mcp__voice_agent__google_calendar__list_events",
            "Checking your calendar",
        ),
        ("google_calendar__list_events", "Checking your calendar"),
        ("mcp__voice_agent__gmail__create_draft", "Drafting an email"),
        ("github__create_pull_request", "Opening a pull request"),
        ("google_docs__append_text", "Updating your Docs"),
        ("mcp__voice_agent__list_threads", "Checking your other threads"),
        ("mcp__voice_agent__save_to_drive", "Saving to your Drive"),
        ("mcp__voice_agent__someone_new__do_thing", "Using a connected app"),
        ("", "Using a connected app"),
    ],
)
def test_connected_app_calls_read_as_plain_language(name, line):
    assert mcp_activity(name) == line
