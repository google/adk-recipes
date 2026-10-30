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

"""Saving to Drive lives on the computer (`voice_agent.save_to_drive`).

The user's setting (`SAVE_TO_DRIVE`, on by default) is their standing approval; the
tool is listed only when it's on and Google is connected. The
server uploads the file from the thread's working directory into the project's Drive
folder, converting by type, updates the same file on a repeat save, and the thread
summary lists what was saved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.api.serializers import thread_summary
from app.app_utils.http_client import set_default_http_transport
from app.auth.credential_store import set_credential_store
from app.auth.identity import run_as
from app.threads import documents
from app.threads.model import ThreadState
from app.threads.prompts import handoff_summary, system_prompt
from app.voice.preferences import get_runtime_preferences, save_preferences
from tests.fake_integrations import FakeDrive, google_token_transport
from tests.fakes import InMemoryCredentialStore, Lab, configure_apps, serve_mcp

S = "https://www.googleapis.com/auth/"
ITINERARY = "# Portland itinerary\n\n## Day 1\n\n- Powell's Books\n"
XLSX = bytes(range(256))  # stands in for a binary workbook


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    """The dev user connected to Google Workspace, with a Drive that keeps its files."""
    from app.auth.providers import require_provider

    configure_apps(monkeypatch, "google_workspace")
    store = InMemoryCredentialStore()
    store.users["local"] = {
        "GOOGLE_WORKSPACE_REFRESH_TOKEN": "local-refresh",
        "GOOGLE_WORKSPACE_TOKEN_SOURCE": "oauth",
    }
    set_credential_store(store)
    fake = FakeDrive(
        google_token_transport(
            {"local-refresh": require_provider("google_workspace").scopes}
        )
    )
    set_default_http_transport(fake.transport())
    return fake


@pytest_asyncio.fixture
async def mcp_lab(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Lab]:
    """A lab whose harness runs reach the MCP bridge over HTTP, as on a computer."""
    async with serve_mcp() as base_url:
        monkeypatch.delenv("VOICE_AGENT_MCP_BASE_URL", raising=False)
        monkeypatch.setenv("APP_URL", base_url)
        yield lab


def _replies(lab: Lab) -> list[dict[str, Any]]:
    return [c["reply"]["result"] for c in lab.agent.mcp_calls]


@pytest.mark.asyncio
async def test_the_setting_and_when_saving_is_available(drive: FakeDrive):
    with run_as("local"):
        assert get_runtime_preferences()["save_to_drive"] == "on"
        assert documents.drive_saving() == documents.DriveSaving(available=True)
        await save_preferences(save_to_drive="off")
        off = documents.drive_saving()
        assert not off.available and "Settings" in off.reason
        await save_preferences(save_to_drive="on")
        with pytest.raises(ValueError, match="save_to_drive"):
            await save_preferences(save_to_drive="never")

    with run_as("someone-else"):
        unconnected = documents.drive_saving()
        assert (
            not unconnected.available and "Connect Google" in unconnected.reason
        )


def test_what_each_file_becomes():
    assert documents.drive_kind("notes/itinerary.md") == (
        "doc",
        "text/markdown",
    )
    assert documents.drive_kind("page.html")[0] == "doc"
    assert documents.drive_kind("budget.csv") == ("sheet", "text/csv")
    assert documents.drive_kind("budget.xlsx")[0] == "sheet"
    assert documents.drive_kind("deck.pptx")[0] == "slides"
    assert documents.drive_kind("map.pdf") == ("file", "application/pdf")
    assert documents.project_title("projects/portland-trip") == "Portland trip"
    assert (
        documents.project_title("scratch/2026-10-25-hello-note") == "Hello note"
    )


@pytest.mark.asyncio
async def test_save_to_drive_is_listed_only_when_available(
    mcp_lab: Lab, drive: FakeDrive
):
    lab = mcp_lab
    lab.agent.script({"steps": [{"save_to_drive": {"path": "missing.md"}}]})
    with run_as("local"):
        await save_preferences(save_to_drive="off")
    handle = await lab.dispatch("Plan a Portland weekend", "projects/portland")
    assert handle.state is ThreadState.COMPLETED, handle.error
    (refused,) = _replies(lab)
    assert (
        refused["isError"]
        and "isn't available" in refused["content"][0]["text"]
    )
    assert drive.files == {}

    from app.api.mcp import McpTokenGrant, _list_tools

    grant = McpTokenGrant(
        token="t", thread_id=handle.thread_id, user_id="local"
    )
    assert "save_to_drive" not in {t["name"] for t in await _list_tools(grant)}
    with run_as("local"):
        await save_preferences(save_to_drive="on")
    assert "save_to_drive" in {t["name"] for t in await _list_tools(grant)}


@pytest.mark.asyncio
async def test_the_computer_saves_into_the_projects_folder_and_updates_in_place(
    mcp_lab: Lab, drive: FakeDrive
):
    lab = mcp_lab
    lab.agent.script(
        {
            "steps": [
                {"write": {"itinerary.md": ITINERARY}},
                {"save_to_drive": {"path": "itinerary.md"}},
                {"run": "printf 'a,b\\n1,2\\n' > budget.csv"},
                {"save_to_drive": {"path": "budget.csv", "title": "Budget"}},
            ]
        },
        {
            "steps": [
                {"write": {"itinerary.md": ITINERARY + "\n## Day 2\n"}},
                {"save_to_drive": {"path": "itinerary.md"}},
            ]
        },
    )
    handle = await lab.dispatch(
        "Plan a Portland weekend", "projects/portland-trip"
    )
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert all(not r["isError"] for r in _replies(lab)), _replies(lab)

    root = drive.named("ADK Voice Agent")
    folder = drive.named("Portland trip")
    assert root["mimeType"] == folder["mimeType"] == documents.FOLDER_MIME
    assert folder["parents"] == [
        next(i for i, f in drive.files.items() if f is root)
    ]
    doc = drive.named("itinerary")
    assert doc["mimeType"] == documents.GOOGLE_MIME["doc"]
    assert doc["content"] == ITINERARY.encode()
    assert doc["content_type"].startswith("text/markdown")
    sheet = drive.named("Budget")
    assert sheet["mimeType"] == documents.GOOGLE_MIME["sheet"]
    assert (
        doc["parents"]
        == sheet["parents"]
        == [next(i for i, f in drive.files.items() if f is folder)]
    )

    expected = [
        {"title": "itinerary", "url": _url(drive, doc), "kind": "doc"},
        {"title": "Budget", "url": _url(drive, sheet), "kind": "sheet"},
    ]
    assert handle.outputs == expected
    assert thread_summary(handle)["links"] == [
        {"kind": o["kind"], "title": o["title"], "url": o["url"]}
        for o in expected
    ]
    assert "Made for the user: itinerary" in handoff_summary(handle)

    # A follow-up saves the itinerary again: the same Drive file, updated.
    await lab.registry.steer(handle.thread_id, "Add a second day")
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert len(drive.files) == 4, "two folders, one Doc, one Sheet"
    assert drive.named("itinerary")["content"].endswith(b"## Day 2\n")
    assert handle.outputs == expected


@pytest.mark.asyncio
async def test_a_kept_one_off_keeps_its_drive_folder_and_files_under_its_new_name(
    mcp_lab: Lab, drive: FakeDrive
):
    from app.projects import name_project

    lab = mcp_lab
    lab.agent.script(
        {
            "steps": [
                {"write": {"itinerary.md": ITINERARY}},
                {"save_to_drive": {"path": "itinerary.md"}},
            ]
        },
        {
            "steps": [
                {"write": {"itinerary.md": ITINERARY + "\n## Day 2\n"}},
                {"save_to_drive": {"path": "itinerary.md"}},
            ]
        },
    )
    handle = await lab.dispatch("Plan a Portland weekend", "")
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert handle.project.startswith("scratch/")
    assert len(drive.files) == 3, (
        "the root folder, the one-off's folder, one Doc"
    )

    kept = await name_project(handle.project, "Portland trip")
    assert kept == "projects/portland-trip"
    folder = drive.named("Portland trip")
    assert folder["mimeType"] == documents.FOLDER_MIME, "renamed in Drive too"

    # The kept project's next save updates the same Doc in the same folder.
    await lab.registry.steer(handle.thread_id, "Add a second day")
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert handle.project == kept
    assert len(drive.files) == 3
    assert drive.named("itinerary")["content"].endswith(b"## Day 2\n")


@pytest.mark.asyncio
async def test_binary_and_other_files_upload_byte_for_byte(
    mcp_lab: Lab, drive: FakeDrive, tmp_path
):
    lab = mcp_lab
    lab.agent.script(
        {
            "steps": [
                {"run": f"cp {tmp_path / 'budget.xlsx'} budget.xlsx"},
                {"save_to_drive": {"path": "budget.xlsx"}},
                {"run": "printf '%%PDF-1.4' > map.pdf"},
                {"save_to_drive": {"path": "map.pdf"}},
            ]
        }
    )
    (tmp_path / "budget.xlsx").write_bytes(XLSX)
    handle = await lab.dispatch("Budget the trip", "projects/portland-trip")
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert all(not r["isError"] for r in _replies(lab)), _replies(lab)

    sheet = drive.named("budget")
    assert sheet["content"] == XLSX
    assert sheet["mimeType"] == documents.GOOGLE_MIME["sheet"]
    pdf = drive.named("map.pdf")
    assert "mimeType" not in pdf, "a plain file keeps its own type"
    assert pdf["content"] == b"%PDF-1.4"
    assert [f["kind"] for f in handle.outputs] == ["sheet", "file"]


@pytest.mark.asyncio
async def test_files_outside_the_working_directory_are_refused(
    mcp_lab: Lab, drive: FakeDrive
):
    lab = mcp_lab
    lab.agent.script(
        {
            "steps": [
                {"save_to_drive": {"path": "../../secrets.txt"}},
                {"run": "mkdir -p .git && echo x > .git/config2"},
                {"save_to_drive": {"path": ".git/config2"}},
            ]
        }
    )
    handle = await lab.dispatch("Save the notes", "projects/notes")
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert [r["isError"] for r in _replies(lab)] == [True, True]
    assert drive.files == {} and handle.outputs == []


@pytest.mark.asyncio
async def test_a_missing_drive_write_scope_asks_to_grant_access():
    from app.auth.providers import GRANT_ACCESS_HINT, require_provider

    # Saving needs `drive.file`; `drive.readonly` alone makes Drive answer 403.
    assert f"{S}drive.file" in require_provider("google_workspace").scopes
    denied = httpx.MockTransport(
        lambda _req: httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Request had insufficient authentication scopes.",
                    "status": "PERMISSION_DENIED",
                }
            },
        )
    )
    async with httpx.AsyncClient(transport=denied) as client:
        with pytest.raises(documents.WorkspaceWriteError) as raised:
            await documents.upload_drive_file(
                client, "Plan", b"x", content_type="text/markdown"
            )
    assert raised.value.needs_grant
    assert GRANT_ACCESS_HINT in str(raised.value)


def test_the_prompts_leave_saving_to_the_computer():
    args = {
        "project": "projects/p",
        "cwd": "/w/projects/p",
        "home": "/w",
        "branch": "",
    }
    prompt = system_prompt(**args)
    assert "Google Docs" not in prompt and "YOUR DOCUMENT" not in prompt


def _url(drive: FakeDrive, file: dict[str, Any]) -> str:
    file_id = next(i for i, f in drive.files.items() if f is file)
    return f"https://drive.google.com/file/d/{file_id}/view"
