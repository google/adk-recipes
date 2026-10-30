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

"""The debug trace (``core.trace``): off by default; when on, every step of
a run lands in its folder with the variation, attempt and retry."""

import asyncio
import base64
import datetime
import json
import logging
import shutil
import threading
from types import SimpleNamespace

import pytest
from google.genai import types

from genmedia4commerce import tools
from genmedia4commerce.core import clients, gen, media, retry, trace
from genmedia4commerce.core._json import UnparsableJSON
from genmedia4commerce.core.errors import DeadlineExceeded
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import outfit
from tests.conftest import trace_file, trace_lines, tracing

PRODUCT = png(64, 64, (20, 40, 60))
PERSON = png(96, 128, (230, 200, 180))
FACE = png(16, 16, (1, 2, 3))
GENERATED = png(12, 16, (120, 0, 0))
CLASSIFIED = {
    "photos": [{"c": "top", "d": "top item", "s": "front", "w": False, "g": 1}]
}
JSON_ANSWERS = {"product classifier": CLASSIFIED}


def _response(part: types.Part) -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=[part])
    candidate = types.Candidate(content=content, finish_reason="STOP")
    return types.GenerateContentResponse(candidates=[candidate])


class FakeModels:
    """``generate_content``: an image for image calls, else JSON chosen by
    the system prompt. Each queued error is raised by one call first."""

    def __init__(self):
        self.errors: list[Exception] = []
        self._lock = threading.Lock()

    def generate_content(self, *, model, contents, config):
        with self._lock:
            error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        if config.response_modalities == ["IMAGE"]:
            part = types.Part.from_bytes(data=GENERATED, mime_type="image/png")
            return _response(part)
        system = config.system_instruction or ""
        answer = next(a for key, a in JSON_ANSWERS.items() if key in system)
        return _response(types.Part.from_text(text=json.dumps(answer)))

    def embed_content(self, *, model, contents, config):
        values = types.ContentEmbedding(values=[1.0, 0.0])
        return types.EmbedContentResponse(embeddings=[values])


@pytest.fixture
def models(monkeypatch) -> FakeModels:
    """A fake genai client behind ``clients.get_client``; no retry sleeps."""
    fake = FakeModels()
    client = SimpleNamespace(models=fake)
    monkeypatch.setattr(clients, "get_client", lambda **_: client)
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    return fake


@pytest.fixture
def tryon_media(monkeypatch):
    """Fake the face crop and the product judge of the try-on."""
    monkeypatch.setattr(media, "face_box", lambda data: (1, 1, 5, 5))
    monkeypatch.setattr(media, "crop_box", lambda data, box, **_: FACE)
    monkeypatch.setattr(
        outfit,
        "evaluate_outfit",
        lambda products, image, **_k: Verdict(True, description="ok"),
    )


def _try_on(**kw) -> dict:
    b64 = base64.b64encode
    return asyncio.run(
        tools.try_on(
            [b64(PRODUCT).decode()],
            b64(PERSON).decode(),
            generate_back=False,
            num_variations=2,
            **kw,
        )
    )


# ------------------------------------------------------------------ off


def test_off_by_default_writes_nothing(models, tryon_media, tmp_path):
    assert get_settings().debug_dir is None
    assert trace.start("abcdef123456", workflow="t", session_id="s") is None
    assert trace.TRACE.get() is None
    assert trace.save("x", b"data") is None
    with trace.scope(variation=0):
        assert trace.TRACE.get() is None
    stream = object()
    assert trace.scoped(stream, stage="image") is stream
    with trace.call("m", ["p"]) as rec:
        assert rec is trace.NO_CALL
        rec.retry = 3  # accepted and ignored
    result = _try_on()
    assert len(result["variations"]) == 2
    assert not list(tmp_path.rglob("trace.jsonl"))


# --------------------------------------------------- a whole try-on run


def test_tryon_run_is_traced(models, tryon_media, debug_dir):
    result = _try_on()
    assert [v["status"] for v in result["variations"]] == ["ready"] * 2
    day = datetime.datetime.now(datetime.UTC).date().isoformat()
    root = debug_dir / day / result["run"]["run_id"]
    run = json.loads((root / "run.json").read_text())
    assert (run["workflow"], run["session_id"]) == ("tryon", "no-session")
    assert run["models"]["image"] == get_models().image
    assert run["settings"]["output_mode"] == "inline"
    request = json.loads(trace_file(root, "_request.json").read_text())
    assert request["product_images"] == [f"<image/png {len(PRODUCT)} bytes>"]
    assert request["policy"]["num_variations"] == 2
    inputs = trace_file(root, "_inputs-product_images-0.png")
    assert inputs.read_bytes() == PRODUCT
    assert trace_file(root, "_inputs-person_image-0.png").read_bytes() == PERSON
    classification = trace_file(root, "_image_classification.json")
    assert json.loads(classification.read_text())["mode"] == "single"
    assert trace_file(root, "_image_wearer-body.png").read_bytes() == PERSON
    assert trace_file(root, "_image_face-crop.png").read_bytes() == FACE
    for i in range(2):
        tag = f"_v{i}a1_image_front_"
        prompt = trace_file(root, f"{tag}request.txt").read_text()
        assert prompt.startswith("--- SYSTEM ---\nYou are an expert")
        assert "--- PART 0 ---" in prompt
        assert list(root.glob(f"*{tag}request-input-*.png"))
        response = trace_file(root, f"{tag}response.png")
        assert response.read_bytes() == GENERATED
        verdict = json.loads(trace_file(root, f"{tag}verdict.json").read_text())
        assert verdict["status"] == "ready"
        assert verdict["verdict"]["publish"] is True
    lines = trace_lines(root)
    assert [line["seq"] for line in lines] == list(range(1, len(lines) + 1))
    assert all((root / x["file"]).exists() for x in lines if x["file"])
    responses = [
        x for x in lines if x["file"] and "front_response" in x["file"]
    ]
    assert sorted(x["labels"]["variation"] for x in responses) == [0, 1]
    for x in responses:
        labels = {"stage": "image", "view": "front", "attempt": 1}
        assert x["labels"] == {**labels, "variation": x["labels"]["variation"]}
        assert x["meta"]["model"] == get_models().image
        assert x["meta"]["retry"] == 0
        assert x["meta"]["finish"] == "STOP"
        request_line = lines[x["meta"]["call"] - 1]
        assert request_line["label"] == "request"
        assert request_line["labels"] == x["labels"]


def test_retry_number_in_the_response_meta(models, debug_dir):
    models.errors = [ConnectionError("connection reset")]
    with tracing() as root:
        assert gen.generate_image(["make it"]) == GENERATED
    error, response = (
        x for x in trace_lines(root) if x["label"].startswith("response")
    )
    assert error["label"] == "response-error"
    assert error["meta"]["retry"] == 0
    assert "connection reset" in (root / error["file"]).read_text()
    assert response["meta"]["retry"] == 1
    assert response["meta"]["duration_ms"] >= 0
    assert response["file"].endswith("_response.png")
    assert error["meta"]["call"] == response["meta"]["call"] == 1


def test_an_unreadable_answer_is_kept_whole(models, debug_dir):
    answer = '{"items": [' + "x" * 300 + "]"  # its message keeps 200 chars
    models.errors = [
        UnparsableJSON(f"unparsable JSON: {answer[:200]!r}", answer)
    ]
    with tracing() as root:
        assert gen.generate_image(["make it"]) == GENERATED
    error = next(x for x in trace_lines(root) if x["label"] == "response-error")
    assert (
        (root / error["file"]).read_text().endswith(f"FULL ANSWER:\n{answer}")
    )


def test_a_stalled_call_is_a_new_attempt_of_the_same_call(models, debug_dir):
    models.errors = [DeadlineExceeded("image: no answer within 90s")]
    with tracing() as root:
        assert gen.generate_image(["make it"]) == GENERATED
    error, response = (
        x for x in trace_lines(root) if x["label"].startswith("response")
    )
    assert (error["meta"]["retry"], response["meta"]["retry"]) == (0, 1)
    assert "no answer within 90s" in (root / error["file"]).read_text()
    assert error["meta"]["call"] == response["meta"]["call"] == 1


def test_json_response_and_request_text(models, debug_dir):
    with tracing() as root:
        gen.generate_json(["describe", PRODUCT], system="product classifier")
    names = sorted(p.name for p in root.iterdir())
    assert names[0].endswith("_request.txt")
    assert names[1].endswith("_request-input-1.png")
    assert names[2].endswith("_response.json")
    text = (root / names[0]).read_text()
    assert text == (
        "--- SYSTEM ---\nproduct classifier\n--- PART 0 ---\ndescribe\n"
        f"--- PART 1 ---\n<image/png {len(PRODUCT)} bytes> request-input-1\n"
    )
    assert json.loads((root / names[2]).read_text()) == CLASSIFIED
    params = trace_lines(root)[0]["meta"]["params"]
    assert params["response_mime_type"] == "application/json"
    assert "system_instruction" not in params


def test_image_parts_carry_their_size_as_sent(models, debug_dir):
    big = png(3000, 2000)  # 6 MP: scaled to 5.16 MP at most, sides % 16
    with tracing() as root:
        gen.generate_json(
            ["describe", PRODUCT, big], system="product classifier"
        )
    inputs = [x for x in trace_lines(root) if x["label"].startswith("request-")]
    assert [x["meta"]["sent_px"] for x in inputs] == [[64, 64], [2768, 1840]]
    assert inputs[1]["file"].endswith("_request-input-2.jpg")


def test_embed_writes_only_a_line(models, debug_dir):
    with tracing() as root:
        gen.embed(["a", "b"], model="emb")
    (line,) = trace_lines(root)
    assert (line["label"], line["file"]) == ("embed", None)
    assert line["meta"] == {"model": "emb", "shape": [2, 2]}
    assert sorted(p.name for p in root.iterdir()) == ["run.json", "trace.jsonl"]


# ------------------------------------------------------- labels and scopes


def test_scope_nesting_merges_labels(debug_dir):
    with tracing() as root:
        with trace.scope(variation=1, attempt=2):
            with trace.scope(stage="video", view="back"):
                inner = trace.save("clip", b"\x00\x00\x00\x18ftypmp42")
            outer = trace.save("verdict", {"ok": True})
        top = trace.save("plan", "a plan")
        trace.save("nothing", None)
    assert inner.name.endswith("_v1a2_video_back_clip.mp4")
    assert outer.name.endswith("_v1a2_verdict.json")
    assert top.name.endswith("_plan.txt")
    assert inner.name.startswith("001_") and top.name.startswith("003_")
    last = trace_lines(root)[-1]
    assert (last["label"], last["file"], last["labels"]) == (
        "nothing",
        None,
        {},
    )


def test_labels_survive_threads_and_pools(debug_dir):
    async def main():
        with trace.scope(variation=0, attempt=1, stage="image"):
            threaded = await asyncio.to_thread(trace.save, "thread", "t")
            with ContextPool(max_workers=2) as pool:
                pooled = pool.submit(trace.save, "pool", "p").result()
        return threaded, pooled

    with tracing():
        threaded, pooled = asyncio.run(main())
    assert threaded.name.endswith("_v0a1_image_thread.txt")
    assert pooled.name.endswith("_v0a1_image_pool.txt")


def test_scoped_stream_labels_each_step_without_leaking(debug_dir):
    seen = []

    async def stream():
        yield trace.save("first", "a")
        await asyncio.to_thread(trace.save, "second", "b")
        yield trace.save("third", "c")

    async def main():
        async for path in trace.scoped(stream(), stage="image"):
            seen.append((path.name, dict(trace.TRACE.get().labels)))

    with tracing() as root:
        asyncio.run(main())
    assert [name.split("_", 2)[2] for name, _ in seen] == [
        "image_first.txt",
        "image_third.txt",
    ]
    assert all(labels == {} for _, labels in seen)  # the consumer's own
    assert trace_file(root, "_image_second.txt")


# ------------------------------------------------------------ failures


def test_unwritable_dir_logs_and_the_run_completes(
    models, tryon_media, monkeypatch, tmp_path, caplog
):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    monkeypatch.setenv("GENMEDIA_DEBUG_DIR", str(blocker))
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger=trace.__name__):
        result = _try_on()
    assert [v["status"] for v in result["variations"]] == ["ready"] * 2
    assert "debug trace off for run" in caplog.text


def test_write_error_is_logged_once_and_stops_the_trace(debug_dir, caplog):
    with caplog.at_level(logging.WARNING, logger=trace.__name__):
        with tracing() as root:
            shutil.rmtree(root)
            assert trace.save("a", "x") is None
            assert trace.save("b", b"y") is None
            with trace.call("m", ["p"]) as rec:
                rec.done("ok", duration_ms=1)
    assert caplog.text.count("debug trace stopped") == 1
