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

"""The Studio agent through real ADK runs, with a scripted model.

Headless turns (``adk web``, the ADK API, ``adk eval``) get their scope
from the ADK session; panel turns from ``run_agent_turn``. Both run on the
Studio's session service over the fake bucket. The fake model records every
request, so the tests see what the model was sent.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.artifacts import InMemoryArtifactService
from google.adk.cli.utils.agent_loader import AgentLoader
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.genai import types
from pydantic import Field

from genmedia4commerce import tools
from genmedia4commerce.core.errors import InputError, StorageError
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import agent as sa
from genmedia4commerce.studio import agent_tools as at
from genmedia4commerce.studio import history, jobs, quota, store, turn
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import prompt as sp
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio.sessions import StudioSessionService

Reply = types.Content | Callable[[LlmRequest], types.Content]


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


#: A run of studio s1 (``core.runs``: runs/<date>/studio-<id>/<run>/).
_RUN = "runs/2026-01-02/studio-s1/0123456789ab"


def upload(name: str, run: str = _RUN) -> str:
    """An upload of studio s1 (or of the studio of ``run``)."""
    return media(f"{run}/inputs/upload-{name}")


def output(name: str) -> str:
    """A result of studio s1."""
    return media(f"{_RUN}/outputs/{name}")


def call(name: str, **args: Any) -> types.Content:
    fc = types.FunctionCall(name=name, args=args)
    return types.Content(role="model", parts=[types.Part(function_call=fc)])


def text(t: str, role: str = "model") -> types.Content:
    return types.Content(role=role, parts=[types.Part(text=t)])


class FakeModel(BaseLlm):
    """Stands in for ``Gemini``: replies from a script, records requests."""

    model: str = "fake"
    script: list[Any] = Field(default_factory=list)
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request.model_copy(deep=True))
        reply = self.script.pop(0)
        content = reply(llm_request) if callable(reply) else reply
        yield LlmResponse(content=content)


@pytest.fixture
def model(storage, monkeypatch) -> FakeModel:
    """The agent's model, scripted by the test; the bucket is fake."""
    del storage
    fake = FakeModel()
    monkeypatch.setattr(sa.root_agent, "model", fake)
    monkeypatch.setattr(
        sp,
        "_GALLERY",
        {"models": "Emma, James", "spin_sets": "Red sneaker (3 photos)"},
    )
    monkeypatch.setattr(sa, "_IMAGES", {"off_until": 0.0})
    return fake


async def headless_turn(
    message: types.Content, session_id: str = "web-1", create: bool = True
) -> Runner:
    """One ``adk web``-style turn: the app on an ADK runner, no scope."""
    runner = Runner(
        app=sa.app,
        session_service=StudioSessionService(),
        artifact_service=InMemoryArtifactService(),
    )
    if create:
        await runner.session_service.create_session(
            app_name=sa.APP_NAME, user_id="u", session_id=session_id
        )
    async for _ in runner.run_async(
        user_id="u", session_id=session_id, new_message=message
    ):
        pass
    return runner


def stored(storage, studio_id: str) -> list[dict]:
    """The contents of the studio's session events, in order."""
    prefix = media(f"sessions/studio-{studio_id}/adk/events/")
    events = [
        json.loads(data)
        for uri, data in sorted(storage.objects.items())
        if uri.startswith(prefix)
    ]
    return [e["content"] for e in events if "content" in e]


def lines(storage, studio_id: str) -> list[str | None]:
    return [
        p.get("text") for c in stored(storage, studio_id) for p in c["parts"]
    ]


def state(storage, studio_id: str) -> dict:
    raw = storage.objects[media(f"sessions/studio-{studio_id}/state.json")]
    return json.loads(raw)["state"]


def responses(req: LlmRequest) -> list[dict]:
    return [
        p.function_response.response
        for c in req.contents
        for p in c.parts or []
        if p.function_response
    ]


# ---- the ADK app ---------------------------------------------------------


def test_adk_loads_the_studio_app_from_the_package():
    """What ``adk web .`` and ``adk eval ./genmedia4commerce`` load."""
    root = Path(__file__).parents[2]
    loaded = AgentLoader(agents_dir=str(root)).load_agent("genmedia4commerce")
    assert loaded is sa.app
    assert loaded.name == "genmedia4commerce"
    assert loaded.root_agent is sa.root_agent


# ---- headless turns --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_headless_turn_gets_its_studio_from_the_adk_session(
    model, storage
):
    model.script = [call("update_settings", scenario="a beach"), text("Done.")]
    await headless_turn(text("put her on a beach", "user"), "web-1")
    # The studio id is the session id; the turn's patches are
    # saved to the studio's state, and the scope is closed.
    assert state(storage, "web-1")["settings"]["tryon"]["scenario"] == "a beach"
    with pytest.raises(LookupError):
        turn.current()
    instruction = model.requests[0].config.system_instruction
    assert "There is no panel" in instruction
    assert "Emma, James" in instruction
    # Without a panel the agent names the options it offers.
    assert "Spin sets: Red sneaker (3 photos)" in instruction
    assert "name the options" in instruction
    texts = lines(storage, "web-1")
    assert texts[0] == "put her on a beach"
    assert texts[1].startswith(sp.STUDIO_NOTE)
    assert texts[-1] == "Done."


@pytest.mark.asyncio
async def test_attached_images_become_studio_uploads(
    model, storage, monkeypatch
):
    saved = []

    def save_upload(image_base64: str, filename: str, studio_id: str = ""):
        saved.append((filename, studio_id))
        ref = media(f"runs/d/studio-{studio_id}/r/inputs/upload-1.png")
        return {
            "ref": ref,
            "label": filename,
            "thumb": "",
            "description": "a white tee",
        }

    monkeypatch.setattr(sm, "save_upload", save_upload)
    model.script = [text("Got it.")]
    image = types.Part(
        inline_data=types.Blob(
            data=b"\x89PNG fake", mime_type="image/png", display_name="tee.png"
        )
    )
    msg = types.Content(
        role="user", parts=[types.Part(text="use this tee"), image]
    )
    await headless_turn(msg, "web2")
    assert saved == [("tee.png", "web2")]
    note = "[studio] The user attached tee.png: a white tee. ref: " + media(
        "runs/d/studio-web2/r/inputs/upload-1.png"
    )
    first = stored(storage, "web2")[0]["parts"]
    assert [p.get("text") for p in first][:2] == ["use this tee", note]
    assert "inline_data" not in json.dumps(stored(storage, "web2"))
    sent = model.requests[0].contents
    assert not any(p.inline_data for c in sent for p in c.parts or [])


@pytest.mark.asyncio
async def test_unreadable_images_are_dropped_for_the_retry_only(
    model, storage, monkeypatch
):
    product = {"label": "tee", "ref": upload("A.jpg")}
    _save_state("web3", {"capability": "tryon", "products": [product]})

    def deny(req: LlmRequest) -> types.Content:
        raise RuntimeError("Permission denied on gs://bucket/c/A.jpg")

    model.script = [deny, text("Here is what I see.")]
    await headless_turn(text("how does it look?", "user"), "web3")
    first, retry = model.requests
    assert any(p.file_data for c in first.contents for p in c.parts or [])
    assert not any(p.file_data for c in retry.contents for p in c.parts or [])
    # Images come back after a while instead of never.
    assert time.monotonic() < sa._IMAGES["off_until"] < time.monotonic() + 601
    assert lines(storage, "web3")[-1] == "Here is what I see."


# ---- tool errors -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_or_unknown_tool_still_gets_a_response(
    model, storage, monkeypatch
):
    async def broken(**_kwargs):
        raise RuntimeError("catalogue down")

    monkeypatch.setattr(tools, "catalog_search", broken)
    model.script = [
        call("find_product", query="black tee"),
        call("no_such_tool"),
        text("Sorry, the catalogue is down."),
    ]
    await headless_turn(text("find a black tee", "user"), "web4")
    failed = responses(model.requests[1])[-1]["error"]
    assert failed == f"find_product failed: {sa._FAILED}"  # not its text
    assert "no_such_tool" in responses(model.requests[2])[-1]["error"]
    names = [
        p["function_response"]["name"]
        for c in stored(storage, "web4")
        for p in c["parts"]
        if "function_response" in p
    ]
    assert names == ["find_product", "no_such_tool"]


# ---- generate and check_generation ----------------------------------------


def fake_start(
    monkeypatch: pytest.MonkeyPatch, job_id: str, specs: list | None = None
) -> None:
    """``generate`` starts job ``job_id`` (past the cost guards); the specs
    it started go to ``specs``."""

    async def start(spec: dict[str, Any]) -> dict[str, Any]:
        if specs is not None:
            specs.append(spec)
        return {"job_id": job_id, "status": "running", "capability": "tryon"}

    monkeypatch.setattr(quota, "start", start)


def scope(st: dict[str, Any], results: dict | None = None) -> turn.Scope:
    sc = turn.Scope(studio_id="s1", st=st, results=results or {})
    turn.start(sc)
    return sc


@pytest.mark.asyncio
async def test_generate_refuses_while_the_studio_has_a_running_job(
    storage, monkeypatch
):
    del storage
    started = []
    fake_start(monkeypatch, "j2", started)
    monkeypatch.setattr(
        jobs, "status", lambda job_id: {"job_id": job_id, "status": "running"}
    )
    scope(
        {
            "capability": "tryon",
            "person": {"label": "me", "ref": upload("me.png")},
            "products": [{"label": "tee", "ref": upload("tee.png")}],
            "job": {"job_id": "j1", "capability": "tryon"},
        }
    )
    res = await at.generate()
    assert "still running" in res["error"]
    assert started == []


RESULTS = [
    {"uri": output("front-0.png"), "score": 91, "note": "faithful"},
    {"uri": output("front-1.png"), "score": 70, "note": "logo off"},
]


@pytest.mark.asyncio
async def test_check_generation_waits_for_the_job_and_returns_the_best(
    monkeypatch,
):
    snaps = iter(
        [
            {"job_id": "j1", "status": "running", "progress": "Judging"},
            {"job_id": "j1", "status": "done", "results": RESULTS},
        ]
    )
    monkeypatch.setattr(jobs, "status", lambda job_id: next(snaps))
    monkeypatch.setattr(at, "_POLL_S", 0.0)
    scope({"job": {"job_id": "j1", "capability": "tryon"}})
    running = await at.check_generation()
    assert running == {
        "job_id": "j1",
        "status": "running",
        "progress": "Judging",
    }
    done = await at.check_generation(wait_s=30)
    assert done["status"] == "done"
    assert done["results"][0] == {
        "ref": RESULTS[0]["uri"],
        "score": 91,
        "view": "",
        "verdict": "faithful",
    }


@pytest.mark.asyncio
async def test_check_generation_without_a_job():
    scope({}, {"capability": "tryon", "results": RESULTS})
    assert (await at.check_generation())["results"][1]["verdict"] == "logo off"
    scope({})
    assert "No generation" in (await at.check_generation())["error"]


@pytest.mark.asyncio
async def test_finished_results_are_artifacts_in_headless_turns_only(
    model, storage, monkeypatch
):
    for r in RESULTS:
        storage.objects[r["uri"]] = b"png-" + r["uri"].encode()
    monkeypatch.setattr(
        jobs,
        "status",
        lambda job_id: {"job_id": job_id, "status": "done", "results": RESULTS},
    )
    job = {"job_id": "j7", "capability": "tryon"}
    _save_state("web5", {"capability": "tryon", "job": job})
    model.script = [
        call("check_generation"),
        call("check_generation"),  # the same job: saved once
        text("It scored 91."),
    ]
    runner = await headless_turn(text("is it done?", "user"), "web5")
    keys = await runner.artifact_service.list_artifact_keys(
        app_name=sa.APP_NAME, user_id="u", session_id="web5"
    )
    assert sorted(keys) == ["j7-front-0.png", "j7-front-1.png"]
    part = await runner.artifact_service.load_artifact(
        app_name=sa.APP_NAME,
        user_id="u",
        session_id="web5",
        filename="j7-front-0.png",
    )
    assert part.inline_data.data == b"png-" + RESULTS[0]["uri"].encode()
    versions = await runner.artifact_service.list_versions(
        app_name=sa.APP_NAME,
        user_id="u",
        session_id="web5",
        filename="j7-front-0.png",
    )
    assert versions == [0]

    # Panel turn: the same tool, no artifacts.
    saved = []
    monkeypatch.setattr(sa, "save_artifacts", lambda *a: saved.append(a))
    _save_state("panel1", {"capability": "tryon", "job": job})
    model.script = [call("check_generation"), text("It scored 91.")]
    _sid, out = await sa.run_agent_turn("is it done?", "panel1")
    assert out.reply == "It scored 91."
    assert saved == []


STUDIO = {
    "capability": "tryon",
    "person": {"label": "me", "ref": upload("me.png")},
    "products": [{"label": "tee", "ref": upload("tee.png")}],
}


@pytest.mark.asyncio
async def test_a_headless_job_is_saved_even_if_the_turn_then_fails(
    model, storage, monkeypatch
):
    fake_start(monkeypatch, "j8")
    _save_state("web6", dict(STUDIO))

    def down(req: LlmRequest) -> types.Content:
        raise RuntimeError("model unavailable")

    model.script = [call("generate"), down]
    with pytest.raises(RuntimeError, match="model unavailable"):
        await headless_turn(text("generate it", "user"), "web6")
    # ADK ran no _after_agent, but generate's job was saved at once, so
    # check_generation finds it on the next turn.
    saved = state(storage, "web6")
    assert saved["job"] == {"job_id": "j8", "capability": "tryon"}
    assert saved["products"] == STUDIO["products"]


@pytest.mark.asyncio
async def test_a_headless_job_saved_twice_is_kept_once(
    model, storage, monkeypatch
):
    fake_start(monkeypatch, "j9")
    _save_state("web7", dict(STUDIO))
    saves = []
    real = ss.save_patched

    def counting(studio_id, patches):
        saves.append(patches)
        return real(studio_id, patches)

    monkeypatch.setattr(ss, "save_patched", counting)
    model.script = [call("generate"), text("Started.")]
    await headless_turn(text("generate it", "user"), "web7")
    job = {"job_id": "j9", "capability": "tryon"}
    # Once by generate, once at the end of the turn: the same job.
    assert saves == [[{"job_started": job}], [{"job_started": job}]]
    saved = state(storage, "web7")
    assert saved["job"] == job
    assert saved["products"] == STUDIO["products"]


@pytest.mark.asyncio
async def test_a_headless_turn_asks_before_generating_what_it_set_up(
    model, storage, monkeypatch
):
    started = []  # through the real cost guards

    def start(spec: dict[str, Any], job_id: str) -> dict[str, Any]:
        started.append(spec)
        return {"job_id": job_id, "status": "running", "capability": "tryon"}

    monkeypatch.setattr(jobs, "start", start)
    run = _RUN.replace("-s1/", "-web9/")  # the job checks they are web9's
    me, tee, cap = (
        upload(name, run) for name in ("me.png", "tee.png", "cap.png")
    )
    products = [{"label": "tee", "ref": tee}]
    _save_state(
        "web9",
        {
            "capability": "tryon",
            "person": {"label": "me", "ref": me},
            "products": [*products, {"label": "cap", "ref": cap}],
        },
    )

    def asks(req: LlmRequest) -> types.Content:
        assert responses(req)[-1] == at.NEEDS_CONFIRMATION
        return text("I removed the cap. Shall I generate?")

    model.script = [call("remove_product", index=1), call("generate"), asks]
    await headless_turn(text("without the cap, and generate", "user"), "web9")
    assert not started
    assert state(storage, "web9")["products"] == products
    model.script = [call("generate"), text("Started.")]
    await headless_turn(text("yes", "user"), "web9", create=False)
    assert [s["products"] for s in started] == [[tee]]
    saved = state(storage, "web9")
    assert saved["job"]["capability"] == "tryon"
    assert len(saved["job_starts"]) == 1


# ---- panel turns -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_panel_turn_without_text_still_replies(model, storage):
    _save_state("p9", {"capability": "tryon", "settings": {}})
    model.script = [call("open_capability", capability="spin"), text("")]
    _sid, out = await sa.run_agent_turn("show the spin page", "p9")
    assert out.reply == "I've updated the studio. Shall I generate?"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (InputError("Add a person first."), "Add a person first."),
        (StorageError("could not read gs://b/sessions/x/state.json"), None),
        (KeyError("secret_key"), None),
    ],
)
async def test_a_failed_tool_tells_the_model_only_user_messages(
    caplog, error, shown
):
    tool = SimpleNamespace(name="use_image")
    answer = await sa._on_tool_error(tool, {}, None, error)
    detail = answer["error"].removeprefix("use_image failed: ")
    assert detail == (shown or sa._FAILED)
    assert str(error) in caplog.text  # the details are logged


def test_the_fallback_reply_says_what_the_turn_did():
    started = [{"job_started": {"job_id": "j1", "capability": "tryon"}}]
    assert sa._fallback_reply(started).startswith("I've started the generation")
    assert sa._fallback_reply([{"capability": "spin"}]).startswith(
        "I've updated"
    )
    assert sa._fallback_reply([]).startswith("Sorry, I lost my reply")


@pytest.mark.asyncio
async def test_a_panel_turn_patches_the_saved_state(model, storage):
    _save_state("p2", {"capability": "tryon", "settings": {}})
    model.script = [call("open_capability", capability="spin"), text("Done.")]
    sid, out = await sa.run_agent_turn("show the spin page", "p2")
    assert sid == "p2"
    assert out.patches == [{"capability": "spin"}]
    assert out.saved_at > 0
    assert state(storage, "p2")["capability"] == "spin"
    instruction = model.requests[0].config.system_instruction
    assert "Studio panel next to this chat" in instruction
    # Chat attachments never reach the Studio: the agent asks for an upload.
    assert "Upload in the panel" in instruction
    # Vague requests get a question first; a spin uses several photos.
    assert "## Ask first, then act" in instruction
    assert "pick in the panel" in instruction
    assert "never use `find_product` for a spin" in instruction
    with pytest.raises(LookupError):
        turn.current()


def user_texts(req: LlmRequest) -> list[str]:
    return [
        p.text
        for c in req.contents
        if c.role == "user"
        for p in c.parts or []
        if p.text
    ]


@pytest.mark.asyncio
async def test_panel_turns_share_the_studios_session_with_notes(model, storage):
    model.script = [text("Hi!"), text("Emma is in the studio.")]
    await sa.run_agent_turn("hello", "p3")
    history.note("p3", "User picked Emma as the model.")
    _sid, out = await sa.run_agent_turn("who is in the studio?", "p3")
    assert out.reply == "Emma is in the studio."
    second = model.requests[1]
    sent = user_texts(second)
    assert sent[0] == "hello"
    assert "[studio] User picked Emma as the model." in sent
    assert sent.index("[studio] User picked Emma as the model.") < sent.index(
        "who is in the studio?"
    )
    assert [c.parts[0].text for c in second.contents if c.role == "model"] == [
        "Hi!"
    ]
    texts = lines(storage, "p3")
    assert texts[-1] == "Emma is in the studio."
    assert texts.count("hello") == 1  # stored once, never rewritten


@pytest.mark.asyncio
async def test_the_instruction_is_static_and_the_studio_goes_in_the_message(
    model, storage
):
    _save_state("p4", {"capability": "tryon"})
    model.script = [text("Ok."), text("Ok.")]
    await sa.run_agent_turn("hi", "p4")
    tee = {"label": "tee", "ref": upload("tee.png")}
    _save_state("p4", {"capability": "tryon", "products": [tee]})
    await sa.run_agent_turn("and now?", "p4")
    first, second = (r.config.system_instruction for r in model.requests)
    assert first == second
    assert upload("tee.png") not in first
    notes = [t for t in user_texts(model.requests[1]) if sp.STUDIO_NOTE in t]
    assert len(notes) == 2  # one per turn: the studio changed
    assert "products: none" in notes[0]
    assert "0: tee (" + upload("tee.png") + ")" in notes[1]


def file_uris(content: dict) -> list[str]:
    return [
        p["file_data"]["file_uri"] for p in content["parts"] if "file_data" in p
    ]


@pytest.mark.asyncio
async def test_images_are_attached_only_when_they_change(model, storage):
    tee = {"label": "tee", "ref": upload("tee.png")}
    _save_state("p5", {"capability": "tryon", "products": [tee]})
    model.script = [text("Ok.")] * 3
    await sa.run_agent_turn("look", "p5")
    await sa.run_agent_turn("and again", "p5")
    cap = {"label": "cap", "ref": upload("cap.png")}
    _save_state("p5", {"capability": "tryon", "products": [cap]})
    await sa.run_agent_turn("and the cap?", "p5")
    users = [c for c in stored(storage, "p5") if c["role"] == "user"]
    assert [file_uris(c) for c in users] == [
        [upload("tee.png")],
        [],  # unchanged: not sent again
        [upload("cap.png")],
    ]
    # The second turn still shows the model the images of the first.
    assert any(p.file_data for c in model.requests[1].contents for p in c.parts)
    assert all("inline_data" not in json.dumps(c) for c in users)


@pytest.mark.asyncio
async def test_a_headless_session_keeps_its_turns_and_state(model, storage):
    model.script = [text("Hello."), text("Still here.")]
    await headless_turn(text("hi", "user"), "web8")
    await headless_turn(text("still there?", "user"), "web8", create=False)
    assert user_texts(model.requests[1])[0] == "hi"
    assert lines(storage, "web8")[-1] == "Still here."


def _save_state(studio_id, state):
    """Store a whole studio state, as the app's actions would have left it."""
    store.write(
        store.state_path(studio_id), {"state": state, "updated_at": 1.0}
    )
