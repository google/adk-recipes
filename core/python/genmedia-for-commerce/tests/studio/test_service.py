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

"""The Studio operations both clients share (``studio.service``)."""

from __future__ import annotations

import base64
import io

import pytest
from google.adk.events import Event
from google.genai import types
from PIL import Image

from genmedia4commerce import tools
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import agent as sa
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import (
    jobs,
    picks,
    prompt,
    service,
    sessions,
    store,
)
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio import turn as studio_turn


@pytest.fixture(autouse=True)
def _bucket(storage, monkeypatch: pytest.MonkeyPatch):
    """State, sessions and jobs in the fake bucket; previews offline."""
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(jobs, "_LOCAL", {})
    monkeypatch.setattr(jobs, "_RUNS", {})
    monkeypatch.setattr(
        sm, "preview_url", lambda uri, video=False: f"https://p/{uri}"
    )
    return storage


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return buf.getvalue()


def _event(author: str, *parts: types.Part) -> Event:
    role = "user" if author == "user" else "model"
    return Event(
        invocation_id=Event.new_id(),
        author=author,
        content=types.Content(role=role, parts=list(parts)),
    )


# ---- agent ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_turn_returns_the_reply_and_the_saved_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_turn(message: str, studio_id: str):
        assert (message, studio_id) == ("make a spin", "s1")
        patches = [{"capability": "spin"}]
        saved_at = ss.save_patched("s1", patches)
        return "s1", studio_turn.AgentTurn(
            reply="Done.", patches=patches, saved_at=saved_at
        )

    monkeypatch.setattr(sa, "run_agent_turn", fake_turn)
    out = await service.agent_turn("make a spin", "s1")
    assert out["studio_id"] == "s1" and out["reply"] == "Done."
    assert out["patches"] == [{"capability": "spin"}]
    assert out["state"]["capability"] == "spin"
    assert out["updated_at"] == out["issued_at"] > 0


@pytest.mark.asyncio
async def test_agent_turn_without_a_save_has_no_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_turn(message: str, studio_id: str):
        del message, studio_id
        return "new1", studio_turn.AgentTurn(reply="Hi.")

    monkeypatch.setattr(sa, "run_agent_turn", fake_turn)
    out = await service.agent_turn("hello")
    assert set(out) == {"studio_id", "reply", "patches", "issued_at"}
    assert out["studio_id"] == "new1"


@pytest.mark.asyncio
async def test_conversation_keeps_what_the_user_typed_and_the_replies() -> None:
    def add(ev: Event) -> None:
        assert sessions.add_event("s1", ev)

    image = types.Part(
        file_data=types.FileData(file_uri=media("runs/a.png"), mime_type="x")
    )
    add(_event("user", types.Part(text="[studio] The user picked a model.")))
    add(
        _event(
            "user",
            types.Part(text="Put her in a red dress"),
            types.Part(text=f"{prompt.STUDIO_NOTE}\nPhoto Try-On"),
            types.Part(text=prompt.IMAGES_NOTE),
            image,
        )
    )
    call = types.FunctionCall(name="find_product", args={"q": "red dress"})
    add(_event("genmedia_studio", types.Part(function_call=call)))
    response = types.FunctionResponse(name="find_product", response={})
    add(_event("genmedia_studio", types.Part(function_response=response)))
    add(
        _event(
            "genmedia_studio",
            types.Part(text="thinking", thought=True),
            types.Part(text="I added a red dress."),
        )
    )
    assert sh.note("s1", "The user removed a product.")

    out = await service.conversation("s1")
    assert out == {
        "studio_id": "s1",
        "messages": [
            {"role": "user", "text": "Put her in a red dress"},
            {"role": "agent", "text": "I added a red dress."},
        ],
    }


@pytest.mark.asyncio
async def test_conversation_of_a_studio_without_a_session_is_empty() -> None:
    assert await service.conversation("nobody") == {
        "studio_id": "nobody",
        "messages": [],
    }


# ---- studio --------------------------------------------------------------------


def test_event_applies_an_action_and_job_finished_needs_a_job_id() -> None:
    run = "runs/2026-01-02/studio-s1/0123456789ab"
    me = {"label": "me", "ref": media(f"{run}/inputs/upload-me.png")}
    out = service.event("s1", {"set": {"field": "person", "item": me}})
    assert out["state"]["person"]["ref"] == me["ref"]
    assert service.state("s1")["state"]["person"]["ref"] == me["ref"]
    with pytest.raises(InputError, match="job_id"):
        service.event("s1", {"job_finished": {}})


def test_job_finished_records_the_jobs_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def finish(studio_id: str, job_id: str, job: dict) -> dict:
        seen.update(studio_id=studio_id, job_id=job_id, job=job)
        return {"state": {}, "updated_at": 1}

    monkeypatch.setattr(ss, "finish_job", finish)
    out = service.event("s1", {"job_finished": {"job_id": "j1"}})
    assert out == {"state": {}, "updated_at": 1}
    assert seen == {
        "studio_id": "s1",
        "job_id": "j1",
        "job": {"job_id": "j1", "status": "unknown"},
    }


def test_upload_takes_base64_or_bytes_with_the_same_checks(
    storage, fake_gen
) -> None:
    fake_gen.text("a red square")
    up = service.upload(_png(), "red.png", "s1")
    assert up["ref"] in storage.uploads and up["label"] == "red.png"
    assert up["description"] == "a red square"
    fake_gen.text("a red square")
    b64 = base64.b64encode(_png()).decode()
    assert service.upload(b64, "", "s1")["label"] == "Upload"
    with pytest.raises(InputError, match="file: not a PNG"):
        service.upload(b"not an image", "x.txt", "s1")


@pytest.mark.asyncio
async def test_catalog_search_returns_thumbnails_and_notes_the_search(
    monkeypatch: pytest.MonkeyPatch, catalog_file
) -> None:
    calls = []

    async def fake_search(query: str, k: int = 5, audience: str = "") -> dict:
        calls.append((query, k, audience))
        return {"items": [{"id": "pb2", "title": "Black leather jacket"}]}

    notes = []
    monkeypatch.setattr(tools, "catalog_search", fake_search)
    monkeypatch.setattr(sh, "note", lambda sid, text: notes.append(text))
    out = await service.catalog_search(" red dress ", "women", "s1")
    assert out["query"] == "red dress" and out["error"] is None
    item = out["items"][0]
    assert (item["label"], item["ref"]) == (
        "Black leather jacket",
        "catalog:pb2",
    )
    assert item["thumb"].startswith("data:image/webp;base64,")
    assert "red dress" in notes[0] and "catalog:pb2" in notes[0]
    out = await service.catalog_search("", "", "s1", inline=False)
    assert out["items"][0]["thumb"] == "/api/catalog/image/pb2/0?w=256"
    assert calls == [("red dress", 8, "women"), ("", 50, "")]
    assert len(notes) == 1  # opening the picker is not a search


@pytest.mark.asyncio
async def test_job_start_refuses_a_bad_spec_and_status_of_unknown() -> None:
    with pytest.raises(InputError, match="capability"):
        await service.job_start({"capability": "x"})
    assert service.job_status("nope") == {"job_id": "nope", "status": "unknown"}


def test_previews_and_gallery(catalog_file) -> None:
    gallery = service.gallery(inline=False)
    assert gallery == {
        "models": [
            {
                "label": "Emma",
                "ref": "catalog:m_emma",
                "thumb": "/api/catalog/image/m_emma/0?w=256",
            }
        ],
        "spin": [
            {
                "label": "Red sneaker",
                "refs": [f"catalog:s_shoe/{n}" if n else "catalog:s_shoe"
                         for n in range(3)],
                "thumb": "/api/catalog/image/s_shoe/0?w=256",
                "thumbs": [f"/api/catalog/image/s_shoe/{n}?w=256"
                           for n in range(3)],
            }
        ],
    }  # fmt: skip
    inline = service.gallery()
    assert inline["models"][0]["thumb"].startswith("data:image/webp;base64,")
    views = inline["spin"][0]["thumbs"]  # each view its own image
    assert views == [
        picks.thumb(r, inline=True) for r in gallery["spin"][0]["refs"]
    ]
    assert views[0] == inline["spin"][0]["thumb"]
    ref = media("runs/2026-01-02/studio-s1/0123456789ab/outputs/front-0.png")
    other_run = media("runs/2026-01-02/web/0123456789ab/outputs/front-0.png")
    assert service.previews([ref, "gs://other/a.png", other_run]) == {
        "previews": {ref: f"https://p/{ref}"}
    }
