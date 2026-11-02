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

"""The Studio MCP App: resource, tools, agent, history and jobs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import re
import time
import uuid
from typing import Any

import mcp.types as mt
import pytest
from PIL import Image

from genmedia4commerce import tools
from genmedia4commerce.core import cache
from genmedia4commerce.core.errors import InputError, StorageError
from genmedia4commerce.core.events import (
    CompleteEvent,
    ErrorEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.mcp import studio
from genmedia4commerce.studio import agent as sa
from genmedia4commerce.studio import agent_spin, job_call, quota, store
from genmedia4commerce.studio import agent_tools as at
from genmedia4commerce.studio import history as studio_history
from genmedia4commerce.studio import jobs as studio_jobs
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import prompt as sp
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio import turn as studio_turn


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


def asset(path: str) -> str:
    """An image the studio can use (an upload of studio s1)."""
    return upload(path.replace("/", "-"))


def _start(spec: dict[str, Any]) -> dict[str, Any]:
    """Start a job past the cost guards (``quota.start``), as a new id."""
    return studio_jobs.start(spec, uuid.uuid4().hex[:10])


def fake_start(
    monkeypatch: pytest.MonkeyPatch, job_id: str, specs: list | None = None
) -> None:
    """The agent's ``generate`` starts job ``job_id`` (past the cost
    guards); the specs it started go to ``specs``."""

    async def start(spec: dict[str, Any]) -> dict[str, Any]:
        if specs is not None:
            specs.append(spec)
        cap = spec["capability"]
        return {"job_id": job_id, "status": "running", "capability": cap}

    monkeypatch.setattr(quota, "start", start)


def saved_studio(studio_id: str) -> None:
    """A studio the app has saved (jobs start only in one)."""
    store.write(
        store.state_path(studio_id),
        {"state": {"capability": "tryon"}, "updated_at": 1.0},
    )


def copy(ref: str) -> str:
    """The fake ``gs://`` copy of a catalogue ref (``_offline``)."""
    return media(f"runs/studio_catalog/{ref.removeprefix('catalog:')}")


@pytest.fixture(autouse=True)
def _fresh_state(storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with an empty fake bucket and no local jobs."""
    del storage  # studio state, results and jobs live in the fake bucket
    monkeypatch.setattr(studio_jobs, "_LOCAL", {})
    monkeypatch.setattr(studio_jobs, "_RUNS", {})
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(studio_jobs, "_FINAL_BACKOFF_S", 0)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, catalog_file) -> None:
    del catalog_file  # the tiny catalogue of tests/conftest.py
    monkeypatch.setattr(
        sm, "preview_url", lambda uri, video=False: f"https://p/{uri}"
    )
    monkeypatch.setattr(
        sm,
        "to_gcs",
        lambda ref: (
            (copy(ref), f"desc of {ref}")
            if ref.startswith("catalog:")
            else (ref, "")
        ),
    )


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sm.resolve`` marks the refs it resolves."""
    monkeypatch.setattr(sm, "resolve", lambda ref: f"R({ref})")


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The ``[studio]`` notes recorded, as (studio id, line)."""
    seen: list[tuple[str, str]] = []

    def note(studio_id: str, text: str) -> bool:
        seen.append((studio_id, studio_history.STUDIO_PREFIX + text))
        return True

    monkeypatch.setattr(studio_history, "note", note)
    return seen


def _history(notes: list[tuple[str, str]], studio_id: str = "s1") -> list:
    """The ``[studio]`` lines noted for a studio."""
    return [line for sid, line in notes if sid == studio_id]


def _sc(res: mt.CallToolResult) -> dict[str, Any]:
    assert res.meta == {"ui": {"resourceUri": studio.STUDIO_URI}}
    return res.structuredContent


@contextlib.contextmanager
def scope(st: dict[str, Any] | None = None, sid: str = "s1"):
    # Like run_agent_turn: the latest results are read once per turn.
    sc = studio_turn.Scope(
        studio_id=sid,
        st=st or {"capability": "tryon"},
        results=ss.last_results(sid),
    )
    token = studio_turn.start(sc)
    try:
        yield sc
    finally:
        studio_turn.end(token)


async def _fake_search(query: str, k: int = 5, audience: str = "") -> dict:
    del k, audience
    return {
        "items": [
            {"id": i, "title": f"{query} {i}", "ref": f"catalog:{i}"}
            for i in ("pa1", "pb2", "pc3")
        ]
    }


# ---- resource and tool list ------------------------------------------------


@pytest.mark.asyncio
async def test_resource_declares_app_mime_and_csp() -> None:
    (res,) = await studio.build_server().list_resources()
    dump = res.model_dump(by_alias=True, exclude_none=True, mode="json")
    assert dump["uri"] == studio.STUDIO_URI
    assert dump["mimeType"] == "text/html;profile=mcp-app"
    csp = dump["_meta"]["ui"]["csp"]["resourceDomains"]
    assert csp == ["https://storage.googleapis.com"]
    html = studio._HTML.read_text(encoding="utf-8")
    for needle in (
        "ui/update-model-context",
        "studio_event",
        "job_started",
        "Make video",  # redo one try-on image's videos
        "From image ",  # each video names its try-on image
        "Try again",
        "Spin sets",  # the spin picker: multi-view sets, no single photos
        "arr(p.thumbs)[i]",  # each view of a spin set shows its own image
    ):
        assert needle in html


@pytest.mark.asyncio
async def test_every_tool_points_at_the_studio_and_one_reaches_the_model() -> (
    None
):
    visibility = {}
    for t in await studio.build_server().list_tools():
        dump = t.model_dump(by_alias=True, exclude_none=True, mode="json")
        assert dump["_meta"]["ui"]["resourceUri"] == studio.STUDIO_URI
        assert dump["annotations"]["readOnlyHint"] is True
        visibility[t.name] = dump["_meta"]["ui"]["visibility"]
    # Gemini Enterprise's model sees a tool only if "model" is listed; the app
    # may call it only if "app" is listed.
    model = {name for name, v in visibility.items() if "model" in v}
    assert model == {"studio_agent"}
    assert all("app" in v for v in visibility.values())
    assert {
        "studio_event",
        "studio_state",
        "studio_preview",
        "studio_gallery",
        "studio_upload",
        "studio_catalog_search",
        "studio_job_start",
        "studio_job_status",
    } == set(visibility) - model


# ---- assistant tool ----------------------------------------------------------


@pytest.mark.asyncio
async def test_studio_agent_relays_and_returns_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    async def fake_turn(message: str, studio_id: str):
        seen.update(message=message, studio_id=studio_id)
        out = studio_turn.AgentTurn(
            reply="Done.", patches=[{"capability": "spin"}]
        )
        return "s1", out

    monkeypatch.setattr(sa, "run_agent_turn", fake_turn)
    res = await studio.studio_agent("make a spin", "s1")
    assert seen == {"message": "make a spin", "studio_id": "s1"}
    assert _sc(res)["patches"] == [{"capability": "spin"}]
    assert json.loads(res.content[0].text) == {
        "studio_id": "s1",
        "reply": "Done.",
    }

    async def broken(message: str, studio_id: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(sa, "run_agent_turn", broken)
    res = await studio.studio_agent("x")
    assert res.isError and "error" in _sc(res)


# ---- app tools -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_search_for_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "catalog_search", _fake_search)
    items = _sc(await studio.studio_catalog_search("rock tee"))["items"]
    assert [i["label"] for i in items] == [
        "rock tee pa1",
        "rock tee pb2",
        "rock tee pc3",
    ]
    assert items[0]["ref"] == "catalog:pa1"
    assert items[0]["thumb"].startswith("data:image/webp;base64,")


# ---- agent tools -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_product_swaps_and_keeps_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "catalog_search", _fake_search)
    with scope(
        {"capability": "tryon", "products": [{"label": "tee", "ref": "x"}]}
    ) as sc:
        res = await at.find_product("black tee", replace_index=0)
        best = {"label": "black tee pa1", "ref": copy("catalog:pa1")}
        assert sc.st["products"] == [best]
        assert res["description"] == "desc of catalog:pa1"
        assert [a["ref"] for a in res["alternatives"]] == [
            "catalog:pb2",
            "catalog:pc3",
        ]
        assert sc.out.patches == [
            {
                "add": {
                    "field": "product",
                    "item": best,
                    "replace_index": 0,
                }
            }
        ]
    with scope():
        assert "error" in await at.find_product(" ")
    # One catalogue photo is too few for a spin: the spin sets instead.
    with scope({"capability": "spin"}) as sc:
        res = await at.find_product("boot")
        assert "choose_spin_set" in res["error"]
        assert res["spin_sets"] == ["Red sneaker"]
        assert sc.out.patches == [] and "spin" not in sc.st


@pytest.mark.asyncio
async def test_choose_spin_set_puts_every_view_on_the_spin_page() -> None:
    with scope() as sc:
        res = await at.choose_spin_set("sneaker")
        views = [
            {"label": f"Red sneaker {n + 1}", "ref": copy(r)}
            for n, r in enumerate(
                ["catalog:s_shoe", "catalog:s_shoe/1", "catalog:s_shoe/2"]
            )
        ]
        assert res == {"spin": views, "description": "desc of catalog:s_shoe"}
        assert sc.st["spin"] == views and sc.st["capability"] == "spin"
        assert sc.out.patches == [
            {"set": {"field": "spin", "items": views}},
            {"capability": "spin"},
        ]
        res = await at.choose_spin_set("blue boot")
        assert res["spin_sets"] == ["Red sneaker"] and "error" in res
        assert len(sc.out.patches) == 2


@pytest.mark.asyncio
async def test_a_catalogue_image_is_never_the_reference_model() -> None:
    with scope({"capability": "tryon"}) as sc:
        res = await at.use_image("catalog:pb2", "reference_model")
        assert "reference model is a photo" in res["error"]
        assert not sc.st.get("reference_model") and not sc.out.patches


@pytest.mark.asyncio
async def test_one_spin_photo_is_accepted_with_a_warning() -> None:
    with scope({"capability": "spin"}) as sc:
        res = await at.use_image("catalog:pb2", "spin")
        assert res["warning"] == agent_spin.ONE_PHOTO
        assert sc.st["spin"] == [
            {"label": "Black leather jacket", "ref": copy("catalog:pb2")}
        ]
        res = await at.use_image(asset("side.png"), "spin")
        assert "warning" not in res and len(sc.st["spin"]) == 2


def test_the_instruction_names_the_gallery_models_and_spin_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sp, "_GALLERY", {})
    text = sp.instruction(headless=True)
    assert "Gallery models: Emma\nSpin sets: Red sneaker (3 photos)" in text
    assert sp.instruction(headless=True) == text


@pytest.mark.asyncio
async def test_use_image_choose_person_and_edits() -> None:
    result = media("runs/2026-10-15/studio-s1/0123456789ab/outputs/front-0.png")
    with scope() as sc:
        assert "error" in await at.use_image("gs://elsewhere/x.png", "person")
        theirs = result.replace("studio-s1", "studio-s2")  # another studio's
        assert "error" in await at.use_image(theirs, "person")
        assert "error" in await at.use_image(result, "shoes")
        # The retired image start: no image to animate any more.
        assert "error" in await at.use_image(result, "tryon_image")
        await at.use_image(result, "person", label="Try-on result")
        assert sc.st["person"] == {"label": "Try-on result", "ref": result}
        res = await at.choose_person("emma")
        assert res["person"] == {"label": "Emma", "ref": copy("catalog:m_emma")}
        assert "models" in await at.choose_person("nobody")
        await at.use_image("catalog:pb2", "product")
        assert sc.st["products"] == [
            {"label": "Black leather jacket", "ref": copy("catalog:pb2")}
        ]
        assert "error" in await at.use_image("catalog:nope", "product")
        assert at.remove_product(5)["error"]
        at.remove_product(0)
        at.clear("person")
        at.update_settings(scenario="street", motion="fly", num_variations=2)
    ops = [next(iter(p)) for p in sc.out.patches]
    assert ops == [
        "set",
        "set",
        "add",
        "remove",
        "clear",
        "settings",
    ]
    assert sc.out.patches[-1] == {
        "settings": {"scenario": "street", "num_variations": "2"}
    }
    assert sc.st["products"] == [] and sc.st["person"] is None


@pytest.mark.asyncio
async def test_generate_starts_a_job_with_the_working_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[dict] = []
    fake_start(monkeypatch, "j1", started)
    with scope() as sc:
        assert "add a person and a product" in (await at.generate())["error"]
        sc.st["person"] = {"label": "me", "ref": upload("me.png")}
        sc.st["products"] = [{"label": "tee", "ref": asset("c/A.jpg")}]
        sc.st["settings"] = {"tryon": {"scenario": "beach"}}
        res = await at.generate()
    assert res == {"status": "started", "job_id": "j1", "capability": "tryon"}
    assert started[0]["person"] == upload("me.png")
    assert started[0]["products"] == [asset("c/A.jpg")]
    assert started[0]["settings"] == {"scenario": "beach"}
    assert sc.out.patches[-1] == {
        "job_started": {"job_id": "j1", "capability": "tryon"}
    }


@pytest.mark.asyncio
async def test_a_video_always_generates_from_the_photos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[dict] = []
    fake_start(monkeypatch, "j3", started)
    me = upload("me.png")
    with scope(
        {
            "capability": "video_tryon",
            "person": {"label": "me", "ref": me},
            "products": [{"label": "tee", "ref": asset("c/A.jpg")}],
        }
    ):
        await at.generate()
    assert (started[0]["capability"], started[0]["person"]) == (
        "video_tryon",
        me,
    )
    assert started[0]["products"] == [asset("c/A.jpg")]
    for key in ("tryon_image", "back_image", "source_run"):
        assert key not in started[0]


@pytest.mark.asyncio
async def test_remake_video_starts_a_make_video_of_image_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[dict] = []
    fake_start(monkeypatch, "j4", started)
    images = [{"uri": output(f"front-{i}.png"), "score": 80} for i in (0, 1)]
    st = {"capability": "video_tryon", "images": {"video_tryon": images}}
    with scope(st) as sc:
        assert "1 to 2" in (await at.remake_video(3))["error"]
        assert "error" in await at.remake_video(0)
        res = await at.remake_video(2)
    assert res == {
        "status": "started",
        "job_id": "j4",
        "capability": "video_tryon",
    }
    # The image only: its run record and back view come from the server.
    assert started[0]["tryon_image"] == output("front-1.png")
    assert "back_image" not in started[0] and "source_run" not in started[0]
    job = {"job_id": "j4", "capability": "video_tryon"}
    job["tryon_image"] = output("front-1.png")
    assert sc.out.patches == [{"job_started": job}]
    with scope({"capability": "video_tryon"}):
        assert (await at.remake_video(1))["error"] == at.NO_IMAGES


# ---- agent: images and history -------------------------------------------------------


def test_image_parts_show_products_sources_and_latest_best() -> None:
    best = output("front-0.png")
    ss.set_last_results("s9", "tryon", [{"uri": best}])
    st = {
        "person": {"ref": upload("me.png")},
        "products": [{"ref": asset(f"c/{i}.jpg")} for i in range(5)]
        + [{"ref": "catalog:pa1"}],
        "spin": [{"ref": output("clip.mp4")}],
    }
    parts = sa.image_parts(st, ss.last_results("s9"))
    uris = [p.file_data.file_uri for p in parts]
    assert uris == [asset(f"c/{i}.jpg") for i in range(5)] + [best]
    assert parts[0].file_data.mime_type == "image/jpeg"
    assert parts[0].media_resolution.level.name == "MEDIA_RESOLUTION_LOW"


def test_summary_lists_refs_and_indexes() -> None:
    text = sa.summary(
        {
            "capability": "tryon",
            "person": {"label": "Emma", "ref": "gs://p"},
            "products": [{"label": "tee", "ref": "gs://t"}],
        }
    )
    assert "person: Emma (gs://p)" in text and "0: tee (gs://t)" in text
    assert "video try-on images: none" in text
    images = [
        {"uri": "gs://f0", "score": 91, "passed": True},
        {"uri": "gs://f1", "score": 60, "passed": False},
    ]
    videos = [{"uri": "gs://v0", "source": "gs://f0"}]
    text = sa.summary(
        {
            "images": {"video_tryon": images},
            "results": {"video_tryon": videos},
        }
    )
    assert (
        "video try-on images: Image 1: gs://f0 (score 91, 1 video(s)); "
        "Image 2: gs://f1 (score 60, didn't pass every check, 0 video(s))"
    ) in text
    defaults = {"complete_look": True, "upscale_crops": False}
    assert f"try-on options: {json.dumps(defaults)}" in text
    st = {"settings": {"tryon": {"complete_look": False}}}
    options = {"complete_look": False, "upscale_crops": False}
    assert f"try-on options: {json.dumps(options)}" in sa.summary(st)


# ---- jobs ------------------------------------------------------------------------------


def test_job_arguments_per_capability(resolved) -> None:
    spec = {
        "capability": "tryon",
        "studio_id": "ab-cd",
        "person": "catalog:m_emma",
        "products": ["gs://m/runs/x.png"],
        "settings": {"scenario": "street", "num_variations": "2"},
    }
    fn, kw = job_call.tool_call(spec)
    assert fn == "try_on"
    assert kw["person_image_base64"] == "R(catalog:m_emma)"
    assert kw["product_images_base64"] == ["R(gs://m/runs/x.png)"]
    assert (kw["scenario"], kw["num_variations"], kw["output"]) == (
        "street",
        2,
        "gcs",
    )
    assert (kw["aspect_ratio"], kw["session_id"]) == ("3:4", "studio-ab-cd")
    video = {**spec, "capability": "video_tryon", "source_run": "gs://m/r"}
    fn, kw = job_call.tool_call({**video, "back_image": "gs://b"})
    assert fn == "video_try_on"
    assert (kw["motion"], kw["aspect_ratio"], kw["back_image_base64"]) == (
        "auto",
        "9:16",
        "R(gs://b)",
    )
    assert kw["source_run"] == ""  # from photos: no image, no source run
    kw = job_call.tool_call({**video, "tryon_image": "gs://m/runs/f.png"})[1]
    assert kw["source_run"] == "gs://m/r"
    fn, kw = job_call.tool_call(
        {"capability": "spin", "studio_id": "s1", "spin": ["gs://a/s.png"]}
    )
    assert fn == "spin_360"
    assert kw["product_images_base64"] == ["R(gs://a/s.png)"]
    assert kw["aspect_ratio"] == "auto"
    assert "collage" not in kw and "complete_look" not in kw


def test_job_passes_the_try_on_options() -> None:
    def options(spec: dict[str, Any]) -> tuple:
        kw = job_call.tool_call(spec)[1]
        assert "collage" not in kw
        return kw["complete_look"], kw["upscale_crops"]

    spec = {"capability": "tryon", "studio_id": "s1", "settings": {}}
    assert options(spec) == (True, False)
    spec["settings"] = {"complete_look": False, "upscale_crops": True}
    assert options(spec) == (False, True)
    assert options({**spec, "capability": "video_tryon"}) == (False, True)
    # A spec from the app is not checked by the patch rules: bad values
    # (and a setting that no longer exists) fall back to the defaults.
    bad = {"complete_look": "no", "upscale_crops": 1, "collage": "true"}
    assert options({**spec, "settings": bad}) == (True, False)


@pytest.mark.parametrize(
    ("settings", "motion"),
    [
        ({"motion": "catwalk"}, "auto"),  # saved before "auto"
        ({"motion": "turnaround"}, "auto"),
        ({"motion": "custom", "prompt": "  "}, "auto"),
        ({"motion": "custom", "prompt": "turns"}, "custom"),
    ],
)
def test_video_job_motion_follows_main(settings, motion) -> None:
    spec = {"capability": "video_tryon", "studio_id": "s1"}
    spec["settings"] = settings
    assert job_call.tool_call(spec)[1]["motion"] == motion


@pytest.mark.asyncio
async def test_update_settings_motion_and_ratios_per_page() -> None:
    with scope({"capability": "video_tryon"}) as sc:
        at.update_settings(motion="catwalk", aspect_ratio="3:4")
        assert sc.out.patches[-1] == {"settings": {}}
        at.update_settings(motion_prompt="walks, then turns")
        vto = sc.st["settings"]["video_tryon"]
        assert (vto["motion"], vto["prompt"]) == ("custom", "walks, then turns")
        at.update_settings(motion="auto", aspect_ratio="16:9")
        assert (vto["motion"], vto["prompt"]) == ("auto", "")
        assert vto["aspect_ratio"] == "16:9"
    with scope({"capability": "spin"}) as sc:
        at.update_settings(aspect_ratio="auto")
        assert sc.st["settings"]["spin"]["aspect_ratio"] == "auto"


@pytest.mark.asyncio
async def test_update_settings_try_on_options() -> None:
    with scope({"capability": "video_tryon"}) as sc:
        res = at.update_settings(complete_look="off", upscale_crops="on")
        expected = {"complete_look": False, "upscale_crops": True}
        assert res == {"settings": expected}
        assert sc.st["settings"] == {"tryon": expected}
        at.update_settings(complete_look="maybe", upscale_crops="off")
        assert sc.out.patches[-1] == {"settings": {"upscale_crops": False}}
        assert sc.st["settings"]["tryon"]["complete_look"] is False


def _variation(index: int, score: float, path: str, **extra: Any):
    return VariationEvent(
        index=index,
        status="ready",
        media=None,
        media_uri=media(path),
        verdict=Verdict(publish=True, description="Publishable"),
        score=score,
        extra=extra,
    )


async def _until(check, tries: int = 200) -> None:
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@pytest.mark.asyncio
async def test_job_streams_results_and_records_inputs_and_outputs(
    notes, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, calls = asyncio.Event(), []

    async def events():
        yield ProgressEvent("preprocessed", "Generating the front view")
        yield _variation(0, 70, f"{_RUN}/outputs/front-1.png", view="front")
        await gate.wait()
        yield _variation(1, 95, f"{_RUN}/outputs/front-0.png", view="front")
        yield CompleteEvent(ranked_indices=[1, 0], best_index=1)

    async def fake_stream(tool: str, **kwargs: Any):
        calls.append((tool, kwargs))
        return events()

    monkeypatch.setattr(tools, "stream", fake_stream)
    saved_studio("s1")
    started = _sc(
        await studio.studio_job_start(
            {
                "studio_id": "s1",
                "capability": "tryon",
                "person": upload("p.png"),
                "products": [upload("t.png")],
                "settings": {"scenario": "beach", "style_guidelines": ""},
            }
        )
    )
    job_id = started["job_id"]

    async def poll() -> dict:
        return _sc(await studio.studio_job_status(job_id))

    await _until(lambda: len(studio_jobs._LOCAL[job_id]["results"]) == 1)
    status = await poll()
    assert (status["status"], status["progress"]) == (
        "running",
        "Generating the front view",
    )
    assert [r["score"] for r in status["results"]] == [70]
    # Another instance reads the same job from the bucket.
    local = studio_jobs._LOCAL.pop(job_id)
    assert (await poll())["results"] == status["results"]
    studio_jobs._LOCAL[job_id] = local
    gate.set()
    await _until(lambda: studio_jobs._LOCAL[job_id]["status"] != "running")
    status = await poll()
    assert status["status"] == "done"
    assert calls[0][0] == "try_on"
    assert [r["score"] for r in status["results"]] == [95, 70]
    assert status["results"][0]["view"] == "front"
    assert ss.last_results("s1")["results"][0]["uri"] == media(
        f"{_RUN}/outputs/front-0.png"
    )
    await _until(lambda: len(_history(notes)) == 2)
    start_line, end_line = _history(notes)
    assert start_line == (
        f"[studio] Started Photo Try-On; person: {upload('p.png')}; "
        f"products: [{upload('t.png')}]; settings: {{'scenario': 'beach'}}."
    )
    assert end_line.startswith(
        f"[studio] Photo Try-On finished. Best: {output('front-0.png')} "
        "(front, score 95, Publishable); others:"
    )
    assert _sc(await studio.studio_job_status("nope"))["status"] == "unknown"
    assert "error" in _sc(await studio.studio_job_start({"capability": "x"}))


@pytest.mark.parametrize(
    ("flag", "refresh"), [(True, True), (None, False), ("true", False)]
)
@pytest.mark.asyncio
async def test_try_again_jobs_run_their_workflow_refreshing_the_cache(
    monkeypatch: pytest.MonkeyPatch, flag: Any, refresh: bool
) -> None:
    seen, kwargs_seen = [], []

    async def events():
        seen.append(cache.refresh_requested())  # as the workflow reads it
        yield _variation(0, 90, f"{_RUN}/outputs/front-0.png", view="front")
        yield CompleteEvent(ranked_indices=[0], best_index=0)

    async def fake_stream(tool: str, **kwargs: Any):
        kwargs_seen.append(kwargs)
        return events()

    monkeypatch.setattr(tools, "stream", fake_stream)
    spec = {"studio_id": "s1", "capability": "tryon", "person": "gs://p"}
    if flag is not None:
        spec["refresh_cache"] = flag
    assert job_call.refresh_cache(spec) is refresh
    job_id = _start(spec)["job_id"]
    await _until(lambda: studio_jobs._LOCAL[job_id]["status"] != "running")
    assert seen == [refresh]
    assert "refresh_cache" not in kwargs_seen[0]  # no tool argument
    assert cache.refresh_requested() is False


@pytest.mark.asyncio
async def test_video_job_shows_the_try_on_image_it_animates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def events():
        yield _variation(
            0, 60, f"{_RUN}/outputs/image-front-0.png", stage="image"
        )
        yield _variation(
            1, 90, f"{_RUN}/outputs/image-front-1.png", stage="image"
        )
        yield ProgressEvent(
            "image_selected",
            "Animating try-on variation 2",
            data={"front_index": 1, "back_index": None},
        )
        yield _variation(0, 88, f"{_RUN}/outputs/video-0.mp4", stage="video")
        yield CompleteEvent(ranked_indices=[0], best_index=0)

    async def fake_stream(tool: str, **kwargs: Any):
        del tool, kwargs
        return events()

    monkeypatch.setattr(tools, "stream", fake_stream)
    spec = {
        "studio_id": "s3",
        "capability": "video_tryon",
        "person": "gs://p",
        "products": ["gs://t"],
    }
    job_id = _start(spec)["job_id"]
    await _until(lambda: studio_jobs._LOCAL[job_id]["status"] != "running")
    job = studio_jobs.status(job_id)
    assert [r["uri"] for r in job["image_results"]] == [
        output("image-front-0.png"),
        output("image-front-1.png"),
    ]
    assert [i["uri"] for i in job["images"]] == [output("image-front-1.png")]
    assert [r["uri"] for r in job["results"]] == [output("video-0.mp4")]


@pytest.mark.asyncio
async def test_job_records_failures(
    notes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_events():
        yield ErrorEvent("quota")

    async def failing(tool: str, **kwargs: Any):
        del kwargs
        if tool == "try_on":
            raise InputError("person_image is required")
        return failing_events()

    monkeypatch.setattr(tools, "stream", failing)
    saved_studio("s2")
    job_id = _sc(
        await studio.studio_job_start(
            {
                "studio_id": "s2",
                "capability": "spin",
                "spin": [upload("a.png", _RUN.replace("-s1/", "-s2/"))],
            }
        )
    )["job_id"]
    await _until(lambda: len(_history(notes, "s2")) == 2)
    status = _sc(await studio.studio_job_status(job_id))
    assert (status["status"], status["error"]) == ("error", "quota")
    assert _history(notes, "s2")[1] == "[studio] 360° Spin failed: quota"
    job_id = _start({"studio_id": "s4", "capability": "tryon"})["job_id"]
    await _until(lambda: studio_jobs._LOCAL[job_id]["status"] != "running")
    assert studio_jobs.status(job_id)["error"] == "person_image is required"


@pytest.mark.asyncio
async def test_the_end_note_is_written_when_the_studio_cannot_be_saved(
    notes, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    async def events():
        yield _variation(0, 80, f"{_RUN}/outputs/front-0.png", view="front")
        yield CompleteEvent(ranked_indices=[0], best_index=0)

    async def fake_stream(tool: str, **kwargs: Any):
        del tool, kwargs
        return events()

    def cannot_save(*_args: Any) -> dict:
        raise StorageError("could not save the studio")

    monkeypatch.setattr(tools, "stream", fake_stream)
    monkeypatch.setattr(ss, "finish_job", cannot_save)
    spec = {"studio_id": "s5", "capability": "tryon", "person": "gs://p"}
    job_id = _start({**spec, "products": ["gs://t"]})["job_id"]
    await _until(lambda: len(_history(notes, "s5")) == 2)
    assert _history(notes, "s5")[1].startswith(
        f"[studio] Photo Try-On finished. Best: {output('front-0.png')}"
    )
    assert studio_jobs.status(job_id)["status"] == "done"
    assert f"Studio job {job_id}: not recorded" in caplog.text


def _blocked_job(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event):
    """``tools.stream`` gives one progress event, then waits for ``gate``."""

    async def events():
        yield ProgressEvent("preprocessed", "Generating")
        await gate.wait()
        yield _variation(0, 90, f"{_RUN}/outputs/front-0.png")

    async def fake_stream(tool: str, **kwargs: Any):
        del tool, kwargs
        return events()

    monkeypatch.setattr(tools, "stream", fake_stream)


def _job_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every job document written, in order."""
    writes: list[dict] = []
    real = store.write

    def write(path: str, doc: Any) -> bool:
        if path.startswith("sessions/studio-jobs/"):
            writes.append(json.loads(json.dumps(doc)))
        return real(path, doc)

    monkeypatch.setattr(store, "write", write)
    return writes


@pytest.mark.asyncio
async def test_job_start_does_no_io_and_status_is_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, writes = asyncio.Event(), _job_writes(monkeypatch)
    _blocked_job(monkeypatch, gate)
    job_id = _start({"studio_id": "s5", "capability": "spin"})["job_id"]
    assert writes == []  # saved by the job's task, off the event loop
    await _until(lambda: writes)
    assert writes[0]["status"] == "running" and writes[0]["updated_at"]
    status = studio_jobs.status(job_id)
    status["results"].append("junk")
    assert studio_jobs.status(job_id)["results"] == []
    gate.set()
    await _until(lambda: not studio_jobs._RUNS)
    assert studio_jobs.status(job_id)["status"] == "done"


@pytest.mark.asyncio
async def test_job_progress_is_throttled_and_a_heartbeat_keeps_it_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(studio_jobs, "_PROGRESS_S", 0.05)
    monkeypatch.setattr(studio_jobs, "_HEARTBEAT_S", 0.15)
    gate, writes = asyncio.Event(), _job_writes(monkeypatch)

    async def events():
        for i in range(50):
            yield ProgressEvent("generating", f"step {i}")
        await gate.wait()
        yield _variation(0, 90, f"{_RUN}/outputs/front-0.png")
        yield CompleteEvent(ranked_indices=[0], best_index=0)

    async def fake_stream(tool: str, **kwargs: Any):
        del tool, kwargs
        return events()

    monkeypatch.setattr(tools, "stream", fake_stream)
    _start({"studio_id": "s6", "capability": "tryon"})
    await _until(lambda: any(w["progress"] == "step 49" for w in writes))
    assert len(writes) < 10  # not one save per progress event
    seen = len(writes)
    await asyncio.sleep(0.4)  # no events: the heartbeat still saves
    assert len(writes) > seen
    assert writes[-1]["updated_at"] > writes[seen - 1]["updated_at"]
    gate.set()
    await _until(lambda: not studio_jobs._RUNS)
    assert writes[-1]["status"] == "done"
    # The variation and the end were saved as they came.
    assert [len(w["results"]) for w in writes[-3:]] == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_job_whose_server_stopped_is_reported_interrupted(
    storage,
) -> None:
    old = {
        "status": "running",
        "capability": "tryon",
        "started": 1000.0,
        "progress": "Generating",
        "results": [],
        "image_results": [],
    }
    store.write(store.job_path("old"), {**old, "updated_at": 1000.0})
    status = studio_jobs.status("old")
    assert (status["status"], status["error"]) == (
        "error",
        studio_jobs.INTERRUPTED,
    )
    fresh = {**old, "updated_at": time.time()}
    store.write(store.job_path("fresh"), fresh)
    assert studio_jobs.status("fresh")["status"] == "running"


@pytest.mark.asyncio
async def test_interrupt_local_saves_running_jobs_as_interrupted(
    storage, notes, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    _blocked_job(monkeypatch, gate)
    job_id = _start({"studio_id": "s8", "capability": "spin"})["job_id"]
    await _until(lambda: _history(notes, "s8"))  # running
    assert await studio_jobs.interrupt_local() == 1
    saved = json.loads(storage.objects[media(store.job_path(job_id))])
    assert (saved["status"], saved["error"]) == (
        "error",
        studio_jobs.INTERRUPTED,
    )
    gate.set()  # the stream ends; the job stays interrupted
    await _until(lambda: not studio_jobs._RUNS)
    assert studio_jobs.status(job_id)["error"] == studio_jobs.INTERRUPTED
    assert await studio_jobs.interrupt_local() == 0


@pytest.mark.asyncio
async def test_a_jobs_last_write_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes, real = [], store.write

    def flaky(path: str, doc: Any) -> bool:
        writes.append(doc["status"])
        if doc["status"] != "running" and writes.count(doc["status"]) < 3:
            return False
        return real(path, doc)

    monkeypatch.setattr(store, "write", flaky)
    monkeypatch.setattr(tools, "stream", _raise_input_error)
    job_id = _start({"studio_id": "s9", "capability": "spin"})["job_id"]
    await _until(lambda: not studio_jobs._RUNS)
    assert writes.count("error") == 3
    saved = store.read(store.job_path(job_id))
    assert (saved["status"], saved["error"]) == ("error", "bad")


async def _raise_input_error(tool: str, **kwargs: Any):
    del tool, kwargs
    raise InputError("bad")


# ---- media -----------------------------------------------------------------------------


def _png() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_upload_is_saved_and_described(storage, fake_gen) -> None:
    fake_gen.text("a red square")
    up = sm.save_upload(_png(), "red.png", studio_id="s-1")
    # A run's inputs/, under the studio's session (as POST /api/uploads).
    pattern = rf"{re.escape(media('runs/'))}\d{{4}}-\d\d-\d\d/studio-s-1/"
    assert re.fullmatch(
        pattern + r"[0-9a-f]{12}/inputs/upload-\w{8}\.png", up["ref"]
    )
    assert up["ref"] in storage.uploads
    assert sm.allowed_ref(up["ref"])
    assert up["description"] == "a red square"
    fake_gen.text(RuntimeError("model down"))
    assert sm.save_upload(_png(), "red.png", "s-1")["description"] == ""


def test_allowed_refs() -> None:
    assert sm.allowed_ref(output("front-0.png"))
    assert sm.allowed_ref(upload("1.png"), "s1")
    assert not sm.allowed_ref(upload("1.png"), "s2")
    assert not sm.allowed_ref(media("runs/x/a.png"))
    assert not sm.allowed_ref(media(f"{_RUN}/manifest.json"))
    assert not sm.allowed_ref(media(f"{_RUN}/record/person.png"))
    assert sm.allowed_ref("catalog:m_emma")
    assert sm.allowed_ref("catalog:s_shoe/2")
    assert not sm.allowed_ref("catalog:s_shoe/3")
    assert not sm.allowed_ref("catalog:nope")
    assert not sm.allowed_ref("gallery:models/w1/a.png")
    assert not sm.allowed_ref(media("sessions/x/history.json"))
    assert not sm.allowed_ref(media("runs/../secret.png"))
    assert not sm.allowed_ref("gs://other/runs/a.png")
    assert not sm.allowed_ref("https://example.com/a.png")


def test_app_serves_the_studio_at_mcp() -> None:
    from genmedia4commerce.api import app as app_module

    paths = [getattr(r, "path", "") for r in app_module.app.router.routes]
    assert "/mcp" in paths
    assert "/a2a" not in paths


@pytest.mark.asyncio
async def test_generate_saves_the_running_job_with_the_studio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_start(monkeypatch, "j9")
    with scope() as sc:
        sc.st["person"] = {"label": "me", "ref": upload("me.png")}
        sc.st["products"] = [{"label": "tee", "ref": asset("c/A.jpg")}]
        await at.generate()
    assert sc.st["job"] == {"job_id": "j9", "capability": "tryon"}
