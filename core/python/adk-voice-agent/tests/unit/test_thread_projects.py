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

"""The thread tool surface over projects: where a thread works, how context is shared across
threads, and what the journal says about a thread's files (contracts R7 and R8)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.api.mcp import McpTokenGrant, _call_list_threads, _call_read_thread
from app.threads.model import ThreadHandle, ThreadState
from app.threads.registry import ThreadRegistry, set_thread_registry
from app.tools.thread_tools import (
    list_projects,
    list_threads,
    share_context,
    start_thread,
)
from app.workers.environment import LocalEnvironment
from app.workers.factory import set_environment
from tests.eval.voice import voice_checks

_GRANT = McpTokenGrant(token="t", thread_id="thread-x", user_id="local")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[ThreadRegistry]:
    """A registry whose threads are recorded but never driven."""
    registry = ThreadRegistry()
    monkeypatch.setattr(registry, "_start", lambda *args, **kwargs: None)
    set_thread_registry(registry)
    yield registry
    set_thread_registry(None)


def _add(registry: ThreadRegistry, thread_id: str, project: str, **fields: Any):
    handle = ThreadHandle(
        thread_id=thread_id,
        goal=fields.pop("goal", "Make a note saying hello"),
        title=fields.pop("title", "Hello note"),
        project=project,
        harness="claude",
        branch=f"agent/{thread_id}",
        owner="local",
        state=fields.pop("state", ThreadState.COMPLETED),
        **fields,
    )
    registry._threads[thread_id] = handle
    return handle


@pytest.mark.asyncio
async def test_an_empty_project_is_a_new_scratch_folder(
    registry: ThreadRegistry,
):
    result = await start_thread("Make a note saying hello", title="Hello note")
    handle = await registry.require(result["thread_id"])
    assert handle.project.startswith("scratch/")
    assert handle.project.endswith("-hello-note")
    assert f"in {handle.project}" in result["message"]


@pytest.mark.asyncio
async def test_a_bare_name_is_a_durable_project(registry: ThreadRegistry):
    result = await start_thread("List what to fix", project="Sell the car")
    handle = await registry.require(result["thread_id"])
    assert handle.project == "projects/sell-the-car"


@pytest.mark.asyncio
async def test_share_context_sends_a_threads_summary_and_paths_to_another_thread(
    registry: ThreadRegistry,
):
    source = _add(
        registry,
        "thread-p1",
        "scratch/2026-10-20-hello-note",
        result_text="Wrote the greeting note.",
        files_changed=["hello.md"],
    )
    target = _add(
        registry,
        "thread-p2",
        "projects/notes",
        title="Collect notes",
    )
    reply = await share_context(
        from_thread_id=source.thread_id,
        to_target=target.thread_id,
        instruction="Summarize this note into the index.",
    )
    assert reply["thread_id"] == target.thread_id
    assert "Shared" in reply["message"]
    sent = target.instructions[-1].text
    ctx = target.instructions[-1].context
    assert sent == "Summarize this note into the index."
    assert (
        'thread thread-p1 "Hello note" in scratch/2026-10-20-hello-note' in ctx
    )
    assert "Wrote the greeting note." in ctx
    assert "hello.md" in ctx

    same_folder = await share_context(
        from_thread_id=source.thread_id,
        to_target="",
        instruction="Check the note for typos.",
    )
    spawned = await registry.require(same_folder["thread_id"])
    assert spawned.thread_id != source.thread_id
    assert spawned.project == source.project


@pytest.mark.asyncio
async def test_dismissed_threads_stay_in_the_journal(registry: ThreadRegistry):
    _add(registry, "thread-d1", "scratch/2026-10-20-hello-note", dismissed=True)
    assert "thread-d1" in await list_threads()
    assert "thread-d1" in await list_threads(query="hello")
    listed = await _call_list_threads(_GRANT, {})
    assert [t["thread_id"] for t in listed["threads"]] == ["thread-d1"]


@pytest.mark.asyncio
async def test_the_journal_says_where_a_threads_files_are(
    registry: ThreadRegistry,
):
    _add(
        registry,
        "thread-n1",
        "scratch/2026-10-20-hello-note",
        workdir="/home/agent/scratch/2026-10-20-hello-note",
        files_changed=["hello.md"],
        artifacts=[{"name": "hello.md", "title": "Hello", "content": "hello"}],
        result_text="Wrote the note.",
    )
    _add(registry, "thread-c1", "code/blog", files_changed=["src/a.py"])

    listed = {
        t["thread_id"]: t
        for t in (await _call_list_threads(_GRANT, {"query": "note"}))[
            "threads"
        ]
    }
    note = listed["thread-n1"]
    assert note["project"] == "scratch/2026-10-20-hello-note"
    assert note["paths"] == ["scratch/2026-10-20-hello-note/hello.md"]
    assert "branch" not in note

    result = await _call_read_thread(_GRANT, {"thread_id": "thread-n1"})
    assert result["workdir"] == "/home/agent/scratch/2026-10-20-hello-note"
    assert result["deliverables"] == [
        {"path": "scratch/2026-10-20-hello-note/hello.md", "title": "Hello"}
    ]
    code = await _call_read_thread(_GRANT, {"thread_id": "thread-c1"})
    assert code["branch"] == "agent/thread-c1"
    assert code["paths"] == ["code/blog/src/a.py"]


@pytest.mark.asyncio
async def test_list_projects_reads_the_computers_folders_newest_first(
    tmp_path: Path,
):
    for rel, mtime in (
        ("code/blog", 100),
        ("projects/sell-the-car", 300),
        ("projects/marathon", 200),
        ("scratch/2026-10-20-hello-note", 400),
        ("scratch/.hidden", 500),
    ):
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        os.utime(folder, (mtime, mtime))
    (tmp_path / "projects" / "stray.md").write_text("not a folder")
    set_environment(LocalEnvironment(tmp_path))
    try:
        listed = await list_projects()
    finally:
        set_environment(None)
    assert "Repositories: code/blog" in listed
    assert listed.index("projects/sell-the-car") < listed.index(
        "projects/marathon"
    )
    assert "scratch/2026-10-20-hello-note" in listed
    assert ".hidden" not in listed and "stray" not in listed


@pytest.mark.asyncio
async def test_list_projects_on_an_empty_home(tmp_path: Path):
    set_environment(LocalEnvironment(tmp_path))
    try:
        assert await list_projects() == "There are no project folders yet."
    finally:
        set_environment(None)


@pytest.mark.parametrize(
    ("actual", "want", "matches"),
    [
        ("code/blog", "blog", True),
        ("blog", "code/blog", True),
        ("", "scratch", True),
        ("workspace", "", True),
        ("scratch/2026-10-20-hello-note", "", True),
        (
            "scratch/2026-10-20-hello-note",
            "scratch/2026-10-20-hello-note",
            True,
        ),
        ("", "scratch/2026-10-20-hello-note", False),
        ("code/blog", "", False),
        ("projects/notes", "code/blog", False),
    ],
)
def test_eval_checks_compare_projects_as_the_server_resolves_them(
    actual: str, want: str, matches: bool
):
    assert voice_checks._project_matches(actual, want) is matches
