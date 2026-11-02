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

"""``generate`` (and ``remake_video``) start a job only with inputs the
user has seen: they refuse when the agent changed them in the same turn,
also from another call of the same model response.

ADK runs the calls of one model response as concurrent tasks; these tests
run the tool functions the same way (``asyncio.gather`` under one scope;
a new scope is the user's next turn) and through real ADK turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from genmedia4commerce.core import catalog
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import agent as sa
from genmedia4commerce.studio import agent_tools as at
from genmedia4commerce.studio import jobs, quota, store, tool_order, turn
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import prompt as sp
from genmedia4commerce.studio import state as ss

SLOW_S = 0.3
WOMAN = {"id": "m_w1", "title": "Studio Woman 01"}


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


def tee() -> str:
    return upload("tee.png")


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Slow catalogue lookups and copies; the job specs ``generate``
    starts."""

    def models() -> list[dict[str, Any]]:
        time.sleep(SLOW_S)
        return [WOMAN]

    def to_gcs(ref: str) -> tuple[str, str]:
        time.sleep(SLOW_S)
        if ref.startswith("catalog:"):
            return media(f"runs/studio_catalog/{ref[8:]}"), "desc"
        return ref, ""

    monkeypatch.setattr(catalog, "models", models)
    monkeypatch.setattr(sm, "to_gcs", to_gcs)
    specs: list[dict[str, Any]] = []

    async def start(spec: dict[str, Any]) -> dict[str, Any]:
        specs.append(spec)
        return {"job_id": "j1", "status": "running", "capability": "tryon"}

    monkeypatch.setattr(quota, "start", start)
    return specs


@contextlib.contextmanager
def scope(st: dict[str, Any] | None = None):
    sc = turn.Scope(studio_id="s1", st=st or {"capability": "tryon"})
    token = turn.start(sc)
    try:
        yield sc
    finally:
        turn.end(token)


def ready() -> dict[str, Any]:
    """A studio whose inputs were set before the turn (by the panel or an
    earlier turn the user saw)."""
    return {
        "capability": "tryon",
        "person": {"label": "me", "ref": upload("me.png")},
        "products": [{"label": "tee", "ref": tee()}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("generate_first", [False, True])
async def test_generate_refuses_after_setup_calls_of_its_response(
    started, generate_first: bool
) -> None:
    with scope() as sc:
        setup = [at.choose_person("woman"), at.use_image(tee(), "product")]
        if generate_first:
            results = await asyncio.gather(at.generate(), *setup)
        else:
            results = await asyncio.gather(*setup, at.generate())
    gen = results[0] if generate_first else results[-1]
    assert gen == at.NEEDS_CONFIRMATION
    assert not started
    assert sc.st["person"]["ref"] == media("runs/studio_catalog/m_w1")
    # The user confirms in the next turn: it generates what they saw.
    with scope(sc.st):
        assert (await at.generate())["status"] == "started"
    assert started[0]["person"] == media("runs/studio_catalog/m_w1")
    assert started[0]["products"] == [tee()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        lambda: at.remove_product(0),
        lambda: at.clear("person"),
        lambda: at.open_capability("video_tryon"),
    ],
    ids=["remove_product", "clear", "page"],
)
async def test_generate_refuses_after_a_sync_input_change(
    started, change
) -> None:
    with scope(ready()):
        change()
        assert await at.generate() == at.NEEDS_CONFIRMATION
    assert not started


@pytest.mark.asyncio
async def test_generate_refuses_after_setup_earlier_in_the_turn(
    started,
) -> None:
    with scope(ready()):
        await at.choose_person("woman")  # the same person, as a new item
        await at.use_image(upload("cap.png"), "product")
        assert await at.generate() == at.NEEDS_CONFIRMATION
    assert not started


@pytest.mark.asyncio
async def test_settings_and_the_same_page_need_no_confirmation(
    started,
) -> None:
    async def settings() -> dict[str, Any]:
        return at.update_settings(scenario="a beach", aspect_ratio="1:1")

    with scope(ready()) as sc:
        assert at.open_capability("tryon") == {"page": "tryon"}
        await asyncio.gather(at.generate(), settings())
    assert not any("capability" in p for p in sc.out.patches)
    assert started[0]["settings"] == {
        "scenario": "a beach",
        "aspect_ratio": "1:1",
    }


@pytest.mark.asyncio
async def test_a_page_switch_in_the_turn_needs_confirmation(started) -> None:
    with scope({**ready(), "capability": "spin"}) as sc:
        at.open_capability("tryon")
        assert await at.generate() == at.NEEDS_CONFIRMATION
    assert not started
    with scope(sc.st):  # the user confirms what they saw
        assert (await at.generate())["status"] == "started"
    assert started[0]["capability"] == "tryon"


@pytest.mark.asyncio
async def test_generate_on_another_page_opens_it_and_asks(started) -> None:
    with scope(ready()) as sc:
        res = await at.generate(capability="video_tryon")
        assert res == at.NEEDS_CONFIRMATION
        assert sc.out.patches == [{"capability": "video_tryon"}]
    assert not started
    with scope(sc.st):  # next turn, on the page the user now sees
        res = await at.generate(capability="video_tryon")
    assert res["status"] == "started"
    assert started[0]["capability"] == "video_tryon"


@pytest.mark.asyncio
async def test_more_variations_need_confirmation_the_same_count_not(
    started,
) -> None:
    with scope(ready()) as sc:
        assert at.update_settings(num_variations=3) == {
            "settings": {"num_variations": "3"}
        }
        assert await at.generate() == at.NEEDS_CONFIRMATION
    assert not started
    with scope(sc.st):  # "yes, 3 variations": nothing changes, it starts
        assert at.update_settings(num_variations=3) == {"settings": {}}
        assert (await at.generate())["status"] == "started"
    assert started[0]["settings"]["num_variations"] == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(tool_order.COST_SETTINGS))
async def test_a_cost_setting_patch_needs_confirmation(started, key) -> None:
    with scope(ready()):
        turn.patch("settings", {key: "1080p"})
        assert await at.generate() == at.NEEDS_CONFIRMATION
    assert not started


@pytest.mark.asyncio
async def test_again_with_unchanged_inputs_starts_at_once(started) -> None:
    st = {**ready(), "settings": {"tryon": {"num_variations": "3"}}}
    with scope({**st}):  # an earlier turn set it up and the user saw it
        assert (await at.generate())["status"] == "started"
    with scope({**st}):  # "again", once that one has finished
        assert (await at.generate(capability="tryon"))["status"] == "started"
    assert len(started) == 2


@pytest.mark.asyncio
async def test_a_setup_call_that_changed_nothing_does_not_count(
    started,
) -> None:
    with scope(ready()):
        assert "error" in await at.choose_person("nobody")
        assert (await at.generate())["status"] == "started"


@pytest.mark.asyncio
async def test_generate_alone_starts_at_once(started) -> None:
    with scope(ready()):
        t0 = time.monotonic()
        res = await at.generate()
        assert time.monotonic() - t0 < 1
    assert res["status"] == "started"
    with scope():
        assert "add a person and a product" in (await at.generate())["error"]


@pytest.mark.asyncio
async def test_setup_in_other_turns_does_not_count(started) -> None:
    gate = asyncio.Event()

    @tool_order.setup_tool
    async def slow() -> None:
        await gate.wait()

    async def other_turn() -> None:
        with scope():
            await slow()

    hung = asyncio.ensure_future(other_turn())
    await asyncio.sleep(0)
    with scope(ready()):
        res = await asyncio.wait_for(at.generate(), 1)
    gate.set()
    await hung
    assert res["status"] == "started"


@pytest.mark.asyncio
async def test_two_generate_calls_of_one_response_start_one_job(
    started, monkeypatch
) -> None:
    status = {"j0": "done", "j1": "running"}  # j0: an earlier job
    monkeypatch.setattr(
        jobs, "status", lambda job_id: {"status": status[job_id]}
    )
    st = {**ready(), "job": {"job_id": "j0", "capability": "tryon"}}
    with scope(st) as sc:
        results = await asyncio.gather(at.generate(), at.generate())
        assert len(started) == 1
        assert [r.get("status") for r in results] == ["started", None]
        assert "still running" in results[1]["error"]
        assert sc.st["job"]["job_id"] == "j1"
        # Once that one has ended, the turn can start another.
        status["j1"] = "done"
        assert (await at.generate())["status"] == "started"
    assert len(started) == 2


def with_images(page: str = "video_tryon") -> dict[str, Any]:
    """A studio on ``page`` whose video try-on made two try-on images."""
    images = [{"uri": output(f"front-{i}.png")} for i in (0, 1)]
    return {**ready(), "capability": page, "images": {"video_tryon": images}}


@pytest.mark.asyncio
async def test_remake_video_asks_after_a_setup_in_its_turn(started) -> None:
    with scope(with_images()) as sc:
        await at.use_image(upload("cap.png"), "product")
        assert await at.remake_video(2) == at.NEEDS_CONFIRMATION
    assert not started
    # The user confirms in the next turn: image 2's videos are made again.
    with scope(sc.st):
        assert (await at.remake_video(2))["status"] == "started"
    assert started[0]["tryon_image"] == output("front-1.png")


@pytest.mark.asyncio
async def test_remake_video_asks_from_another_page(started) -> None:
    with scope(with_images("tryon")) as sc:
        assert await at.remake_video(1) == at.NEEDS_CONFIRMATION
        assert sc.st["capability"] == "video_tryon"  # it opens the page
    assert not started


@pytest.mark.asyncio
async def test_remake_video_waits_for_setup_calls_of_its_response(
    started,
) -> None:
    with scope(with_images()):
        results = await asyncio.gather(
            at.remake_video(1), at.choose_person("woman")
        )
    assert results[0] == at.NEEDS_CONFIRMATION
    assert not started


@pytest.mark.asyncio
async def test_remake_video_and_generate_of_one_response_start_one_job(
    started, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "status", lambda job_id: {"status": "running"})
    with scope(with_images()):
        results = await asyncio.gather(at.remake_video(1), at.generate())
    assert len(started) == 1
    assert [r.get("status") for r in results] == ["started", None]
    assert "still running" in results[1]["error"]


class FakeModel(BaseLlm):
    """Replies from a script, like ``Gemini`` would."""

    model: str = "fake"
    script: list[types.Content] = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(content=self.script.pop(0))


def calls(*named: tuple[str, dict[str, Any]]) -> types.Content:
    """One model response with several function calls."""
    parts = [
        types.Part(function_call=types.FunctionCall(name=n, args=a))
        for n, a in named
    ]
    return types.Content(role="model", parts=parts)


def reply(t: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=t)])


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(sa.root_agent, "model", fake)
    monkeypatch.setattr(sp, "_GALLERY", {"models": "Emma", "spin_sets": "none"})
    monkeypatch.setattr(sa, "_IMAGES", {"off_until": 0.0})
    return fake


@pytest.mark.asyncio
async def test_an_adk_turn_sets_up_and_the_next_one_generates(
    storage, started, model
) -> None:
    del storage
    _save_state("p9", {"capability": "tryon", "settings": {}})
    tee_ref = upload("tee.png", "runs/2026-01-02/studio-p9/0123456789ab")
    model.script = [
        calls(
            ("generate", {}),
            ("choose_person", {"name": "woman"}),
            ("use_image", {"ref": tee_ref, "field": "product"}),
            ("update_settings", {"scenario": "a beach"}),
        ),
        reply("I put a woman and the tee in the studio. Shall I generate?"),
        calls(("generate", {})),
        reply("Started."),
    ]
    _sid, out = await sa.run_agent_turn("try the tee on a woman", "p9")
    assert not started
    assert not any("job_started" in p for p in out.patches)
    _sid, out = await sa.run_agent_turn("yes", "p9")
    assert len(started) == 1
    assert started[0]["person"] == media("runs/studio_catalog/m_w1")
    assert started[0]["products"] == [tee_ref]
    assert started[0]["settings"] == {"scenario": "a beach"}
    assert out.patches == [
        {"job_started": {"job_id": "j1", "capability": "tryon"}}
    ]


@pytest.mark.asyncio
async def test_inputs_the_user_set_in_the_panel_need_no_confirmation(
    storage, started, model
) -> None:
    del storage
    _save_state("p8", {"capability": "tryon", "settings": {}})
    run = "runs/2026-01-02/studio-p8/0123456789ab"
    me = {"label": "me", "ref": upload("me.png", run)}
    ss.apply_action("p8", {"set": {"field": "person", "item": me}})
    tee_ref = upload("tee.png", run)
    tee_item = {"label": "tee", "ref": tee_ref, "replace_index": -1}
    ss.apply_action("p8", {"add": {"field": "product", "item": tee_item}})
    model.script = [calls(("generate", {})), reply("Started.")]
    await sa.run_agent_turn("generate", "p8")
    assert len(started) == 1
    assert (started[0]["person"], started[0]["products"]) == (
        me["ref"],
        [tee_ref],
    )


def _save_state(studio_id, state):
    """Store a whole studio state, as the app's actions would have left it."""
    store.write(
        store.state_path(studio_id), {"state": state, "updated_at": 1.0}
    )
