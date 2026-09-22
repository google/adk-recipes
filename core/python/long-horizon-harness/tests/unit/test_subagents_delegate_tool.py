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

"""Dynamic ``delegate`` tool — the parent's function-tool entry point.

Replaces ``AgentTool(agent=delegate_agent)``. The parent LLM calls
``delegate(goal=..., context=..., toolsets=[...], skills=[...])``;
the tool builds a fresh child via ``build_child_agent`` and runs it
under a state-isolated Runner.

These tests pin the tool's contract — argument handling, error
shapes, and that it delegates the heavy lifting to the builder + runner
(without re-asserting those modules' behavior).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.asyncio


def _write_skill(root: Path, name: str, body: str) -> None:
    from horizon.tools.skill_reload import host_mirror_dir

    fm = yaml.safe_dump({"name": name, "description": "test"}, sort_keys=True)
    content = f"---\n{fm}---\n\n{body}\n"
    skill_dir = root / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    # The delegate builder reads from the per-session host mirror that
    # bind_session_skills_callback populates at runtime; mirror here too.
    mirror_dir = host_mirror_dir(root) / name
    mirror_dir.mkdir(parents=True, exist_ok=True)
    (mirror_dir / "SKILL.md").write_text(content, encoding="utf-8")


@pytest.fixture()
def skills_home(tmp_path: Path):
    import shutil

    from horizon.environment import LocalEnvironment
    from horizon.environment_context import (
        clear_active_environment,
        set_active_environment,
    )
    from horizon.tools.skill_reload import host_mirror_dir

    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    try:
        yield tmp_path
    finally:
        clear_active_environment()
        shutil.rmtree(host_mirror_dir(tmp_path), ignore_errors=True)


async def test_delegate_returns_run_envelope_on_happy_path(
    monkeypatch, skills_home: Path
) -> None:
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s):
        captured["child_name"] = child.name
        captured["user_message"] = user_message
        captured["timeout_s"] = timeout_s
        return {
            "status": "completed",
            "summary": "Found 3 files.",
            "telemetry": {
                "iterations": 2,
                "duration_ms": 123,
                "invocation_id": "abc",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)

    result = await delegate_mod.delegate(
        goal="enumerate files",
        context="under /tmp/foo",
        toolsets=["file"],
        skills=[],
        timeout_s=30,
    )

    assert result["status"] == "completed"
    assert result["summary"] == "Found 3 files."
    assert result["telemetry"]["iterations"] == 2
    assert "system instruction above" in captured["user_message"]
    assert captured["timeout_s"] == 30
    assert captured["child_name"].startswith("delegate_")


async def test_delegate_defaults_when_optional_args_omitted(
    monkeypatch, skills_home: Path
) -> None:
    """``context``, ``toolsets``, ``skills``, ``timeout_s`` all optional;
    omitting them must produce a working child built with defaults."""
    from horizon.subagents import delegate as delegate_mod
    from horizon.tools.processes.terminal import bash
    from horizon.tools.read import ReadTool

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s):
        captured["tools"] = list(child.tools)
        captured["timeout_s"] = timeout_s
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)

    result = await delegate_mod.delegate(goal="do X")

    assert result["status"] == "completed"
    # Default toolsets = file + shell. read_file/view_file were merged into ReadTool.
    assert any(isinstance(t, ReadTool) for t in captured["tools"])
    assert bash in captured["tools"]
    # Default timeout is 120s.
    assert captured["timeout_s"] == 120


async def test_delegate_unknown_toolset_returns_structured_error(
    monkeypatch, skills_home: Path
) -> None:
    """KeyError from the builder must NOT escape — surface a halted
    envelope so the parent LLM can retry with a valid toolset name."""
    from horizon.subagents import delegate as delegate_mod

    called = {"value": False}

    async def fake_run_delegate(**_kwargs: Any) -> dict[str, Any]:
        called["value"] = True
        return {}

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)

    result = await delegate_mod.delegate(goal="x", toolsets=["bogus"])

    assert result["status"] == "halted"
    assert "bogus" in result["summary"]
    assert "toolset" in result["summary"].lower()
    assert called["value"] is False  # never reached the runner


async def test_delegate_passes_skill_content_to_child(
    monkeypatch, skills_home: Path
) -> None:
    _write_skill(skills_home, "gws-drive", body="Run `gws drive files list`.")
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s):
        captured["instruction"] = child.instruction
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)

    await delegate_mod.delegate(
        goal="enumerate Drive docs",
        toolsets=["shell"],
        skills=["gws-drive"],
    )

    assert "gws drive files list" in captured["instruction"]


async def test_delegate_user_message_is_short_fixed_kickoff(
    monkeypatch, skills_home: Path
) -> None:
    """The runner's ``user_message`` is a short fixed kickoff line — the
    goal and context live in the system instruction, so the user turn
    must NOT recap them (avoids duplication and mid-word truncation)."""
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, str] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["user_message"] = user_message
        return {
            "status": "completed",
            "summary": "",
            "telemetry": {
                "iterations": 0,
                "duration_ms": 0,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)

    await delegate_mod.delegate(
        goal="enumerate files under /tmp", context="X" * 5000
    )

    assert len(captured["user_message"]) <= 400
    assert "system instruction above" in captured["user_message"]
    assert "enumerate files under /tmp" not in captured["user_message"]


async def test_delegate_accepts_model_override(
    monkeypatch, skills_home: Path
) -> None:
    """A non-default `model=` flows through to the built child."""
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["model"] = child.model.model
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    await delegate_mod.delegate(
        goal="deep think", model="gemini-3.1-pro-preview"
    )
    assert captured["model"] == "gemini-3.1-pro-preview"


async def test_delegate_rejects_unknown_model_with_structured_error(
    monkeypatch, skills_home: Path
) -> None:
    """An off-allowlist model name must produce a halted envelope, not
    a raw exception leaking into the parent."""
    from horizon.subagents import delegate as delegate_mod

    async def fake_run_delegate(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("runner should not be reached")

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    result = await delegate_mod.delegate(goal="x", model="not-a-real-model")
    assert result["status"] == "halted"
    assert "model" in result["summary"].lower()


async def test_delegate_tools_list_merges_with_toolsets(
    monkeypatch, skills_home: Path
) -> None:
    """``tools=[...]`` adds to the resolved tool list alongside toolsets,
    deduplicated."""
    from horizon.subagents import delegate as delegate_mod
    from horizon.tools.processes.terminal import bash
    from horizon.tools.read import ReadTool

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["tools"] = list(child.tools)
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    await delegate_mod.delegate(
        goal="x", toolsets=["file"], tools=["bash", "read"]
    )
    assert bash in captured["tools"]
    # read_file/view_file were merged into ReadTool; requesting it by its
    # current tool name ("read") resolves to the same ReadTool instance
    # the "file" toolset already carries, so it must not be duplicated.
    read_tools = [t for t in captured["tools"] if isinstance(t, ReadTool)]
    assert len(read_tools) == 1


async def test_delegate_unknown_tool_returns_structured_error(
    monkeypatch, skills_home: Path
) -> None:
    from horizon.subagents import delegate as delegate_mod

    async def fake_run_delegate(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("runner should not be reached")

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    result = await delegate_mod.delegate(
        goal="x", toolsets=None, tools=["definitely_not_a_real_tool"]
    )
    assert result["status"] == "halted"
    assert "definitely_not_a_real_tool" in result["summary"]


async def test_delegate_inline_instructions_appended_to_child_prompt(
    monkeypatch, skills_home: Path
) -> None:
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["instruction"] = child.instruction
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    await delegate_mod.delegate(
        goal="x", instructions="Prefer markdown tables in the summary."
    )
    assert "Prefer markdown tables in the summary." in captured["instruction"]
    assert "Caller instructions" in captured["instruction"]


async def test_delegate_inline_skills_render_alongside_disk_skills(
    monkeypatch, skills_home: Path
) -> None:
    _write_skill(skills_home, "from-disk", body="Disk skill body.")
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["instruction"] = child.instruction
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    await delegate_mod.delegate(
        goal="x",
        skills=["from-disk"],
        inline_skills=[{"name": "inline-one", "body": "Inline body."}],
    )
    assert "Disk skill body." in captured["instruction"]
    assert "Inline body." in captured["instruction"]


async def test_delegate_output_format_json_parses_summary(
    monkeypatch, skills_home: Path
) -> None:
    """``output_format='json'`` parses the child's text into a dict and
    places it in the ``summary`` field of the envelope."""
    from horizon.subagents import delegate as delegate_mod

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        return {
            "status": "completed",
            "summary": '{"answer": "42", "confidence": 0.9}',
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    result = await delegate_mod.delegate(goal="x", output_format="json")
    assert result["status"] == "completed"
    assert result["summary"] == {"answer": "42", "confidence": 0.9}


async def test_delegate_output_format_json_with_code_fences_parses(
    monkeypatch, skills_home: Path
) -> None:
    """Children sometimes wrap JSON in ```json fences despite the
    directive. Strip fences before parsing rather than failing."""
    from horizon.subagents import delegate as delegate_mod

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        return {
            "status": "completed",
            "summary": '```json\n{"answer": "42"}\n```',
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    result = await delegate_mod.delegate(goal="x", output_format="json")
    assert result["summary"] == {"answer": "42"}


async def test_delegate_output_format_json_parse_failure_returns_halted(
    monkeypatch, skills_home: Path
) -> None:
    """When the child returns non-JSON despite the directive, mark the
    envelope ``halted`` and preserve the raw text under ``summary_raw``."""
    from horizon.subagents import delegate as delegate_mod

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        return {
            "status": "completed",
            "summary": "Sorry I couldn't comply.",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    result = await delegate_mod.delegate(goal="x", output_format="json")
    assert result["status"] == "halted"
    assert result["summary_raw"] == "Sorry I couldn't comply."


async def test_delegate_output_schema_flows_to_child_agent(
    monkeypatch, skills_home: Path
) -> None:
    """``output_schema`` is now enforced by ADK via the child agent's
    ``output_schema`` attribute (SetModelResponseTool path) rather than by
    a post-hoc validation pass inside ``delegate()``. Assert the schema
    flows through to the child."""
    from horizon.subagents import delegate as delegate_mod

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["output_schema"] = child.output_schema
        return {
            "status": "completed",
            "summary": '{"answer": "ok"}',
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    result = await delegate_mod.delegate(
        goal="x", output_format="json", output_schema=schema
    )
    assert captured["output_schema"] == schema
    assert result["status"] == "completed"
    assert result["summary"] == {"answer": "ok"}


async def test_delegate_max_iterations_propagates_to_runner(
    monkeypatch, skills_home: Path
) -> None:
    """``max_iterations`` flows through to ``run_delegate`` so the runner
    can enforce the cap."""
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **kwargs):
        captured["max_iterations"] = kwargs.get("max_iterations")
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    await delegate_mod.delegate(goal="x", max_iterations=5)
    assert captured["max_iterations"] == 5


async def test_delegate_custom_name_surfaces_in_child(
    monkeypatch, skills_home: Path
) -> None:
    from horizon.subagents import delegate as delegate_mod

    captured: dict[str, Any] = {}

    async def fake_run_delegate(*, child, user_message, timeout_s, **_kwargs):
        captured["name"] = child.name
        return {
            "status": "completed",
            "summary": "ok",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_mod, "run_delegate", fake_run_delegate)
    await delegate_mod.delegate(goal="x", name="enumerate_docs")
    assert captured["name"].startswith("enumerate_docs_")
