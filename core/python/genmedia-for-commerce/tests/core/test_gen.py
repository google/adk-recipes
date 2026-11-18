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

import asyncio
import base64
import contextlib
import threading

import numpy as np
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from genmedia4commerce.core import clients, gen, retry
from genmedia4commerce.core.errors import (
    DeadlineExceeded,
    GenerationError,
    InputError,
)
from genmedia4commerce.core.gen import ImageParams, TextParams, VideoParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.runner import RunPolicy, run_variations
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import StoredVideo, Verdict


def text_response(text):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text=text)]
                ),
                finish_reason="STOP",
            )
        ]
    )


def image_response(data):
    parts = [
        types.Part(text="thinking...", thought=True),
        types.Part.from_text(text="Here it is"),
        types.Part.from_bytes(data=data, mime_type="image/png"),
    ]
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=parts))
        ]
    )


def empty_response():
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[]),
                finish_reason="SAFETY",
            )
        ]
    )


class Stub:
    """Plays queued responses (or raises queued exceptions) and records calls."""

    def __init__(self):
        self.responses = []
        self.calls = []

    def _next(self, kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def generate_content(self, **kwargs):
        return self._next(kwargs)

    def embed_content(self, **kwargs):
        return self._next(kwargs)

    def create(self, **kwargs):
        return self._next(kwargs)


class StubClient:
    def __init__(self):
        self.models = Stub()
        self.interactions = Stub()


@pytest.fixture
def client(monkeypatch):
    stub = StubClient()
    stub.get_client_calls = []

    def fake_get_client(**kwargs):
        stub.get_client_calls.append(kwargs)
        return stub

    monkeypatch.setattr(clients, "get_client", fake_get_client)
    return stub


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(retry.time, "sleep", recorded.append)
    return recorded


# ---------------------------------------------------------------- text / json


def test_generate_text_config(client):
    client.models.responses = [text_response("  hello  ")]
    params = TextParams(
        model="m-text",
        thinking="HIGH",
        temperature=0.3,
        media_resolution="MEDIA_RESOLUTION_LOW",
        max_output_tokens=123,
    )
    out = gen.generate_text(
        ["describe", png()], system="be brief", params=params
    )
    assert out == "hello"
    call = client.models.calls[0]
    assert call["model"] == "m-text"
    cfg = call["config"]
    assert cfg.temperature == 0.3
    assert cfg.max_output_tokens == 123
    assert cfg.thinking_config.thinking_level == "HIGH"
    assert cfg.media_resolution == "MEDIA_RESOLUTION_LOW"
    assert cfg.system_instruction == "be brief"
    assert cfg.response_mime_type is None
    assert {s.threshold for s in cfg.safety_settings} == {"OFF"}
    assert cfg.http_options.timeout > 0
    parts = call["contents"][0].parts
    assert parts[0].text == "describe"
    assert parts[1].inline_data.mime_type == "image/png"


def test_generate_text_thinking_off(client):
    client.models.responses = [text_response("x")]
    gen.generate_text(["q"], params=TextParams(thinking=None))
    assert client.models.calls[0]["config"].thinking_config is None


def test_generate_text_thinking_off_sets_zero_budget(client):
    client.models.responses = [text_response("x")]
    gen.generate_text(["q"], params=TextParams(thinking="OFF"))
    cfg = client.models.calls[0]["config"].thinking_config
    assert cfg.thinking_budget == 0
    assert cfg.thinking_level is None


def test_generate_text_default_model_from_registry(client, monkeypatch):
    monkeypatch.setenv("GENMEDIA_TEXT_MODEL", "registry-text")
    get_models.cache_clear()
    client.models.responses = [text_response("x")]
    gen.generate_text(["q"])
    assert client.models.calls[0]["model"] == "registry-text"


def test_generate_text_empty_twice_raises(client):
    client.models.responses = [empty_response(), empty_response()]
    with pytest.raises(GenerationError, match="no text"):
        gen.generate_text(["q"])
    assert len(client.models.calls) == 2


def test_generate_json_parses_fenced(client):
    client.models.responses = [text_response('```json\n{"a": [1, 2,],}\n```')]
    schema = {"type": "OBJECT", "properties": {"a": {"type": "ARRAY"}}}
    assert gen.generate_json(["q"], schema=schema) == {"a": [1, 2]}
    cfg = client.models.calls[0]["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is not None


def test_generate_json_extracts_first_block(client):
    client.models.responses = [text_response('Sure! [{"x": 1}] hope it helps')]
    assert gen.generate_json(["q"]) == [{"x": 1}]


def test_generate_json_unparsable_twice(client):
    client.models.responses = [
        text_response("nope"),
        text_response("still {bad"),
    ]
    with pytest.raises(GenerationError, match="unparsable JSON"):
        gen.generate_json(["q"])
    assert len(client.models.calls) == 2


def test_generate_json_unparsable_then_valid(client):
    client.models.responses = [
        text_response("nope"),
        text_response('{"ok": true}'),
    ]
    assert gen.generate_json(["q"]) == {"ok": True}
    assert len(client.models.calls) == 2


# ---------------------------------------------------------------------- image


def test_generate_image_config_and_first_inline_image(client):
    data = png(8, 8)
    client.models.responses = [image_response(data)]
    params = ImageParams(
        model="m-img",
        aspect_ratio="16:9",
        image_size="4K",
        output_mime_type="image/jpeg",
        thinking="LOW",
        media_resolution="MEDIA_RESOLUTION_MEDIUM",
        temperature=0.7,
    )
    out = gen.generate_image(["make it"], system="sys", params=params)
    assert out == data
    call = client.models.calls[0]
    assert call["model"] == "m-img"
    cfg = call["config"]
    assert cfg.response_modalities == ["IMAGE"]
    assert cfg.image_config.aspect_ratio == "16:9"
    assert cfg.image_config.image_size == "4K"
    assert cfg.image_config.output_mime_type == "image/jpeg"
    assert cfg.thinking_config.thinking_level == "LOW"
    assert cfg.media_resolution == "MEDIA_RESOLUTION_MEDIUM"
    assert cfg.temperature == 0.7
    assert cfg.system_instruction == "sys"


def test_generate_image_defaults(client):
    client.models.responses = [image_response(png())]
    gen.generate_image(["x"])
    call = client.models.calls[0]
    assert call["model"] == get_models().image
    cfg = call["config"]
    assert cfg.image_config.aspect_ratio == "3:4"
    assert cfg.image_config.image_size == "2K"
    assert cfg.image_config.output_mime_type == "image/png"
    assert cfg.thinking_config.thinking_level == "MEDIUM"
    assert cfg.media_resolution == "MEDIA_RESOLUTION_HIGH"
    assert cfg.temperature == 0.1


def test_generate_image_auto_ratio_omits_it(client):
    client.models.responses = [image_response(png())]
    gen.generate_image(["x"], params=ImageParams(aspect_ratio="auto"))
    assert client.models.calls[0]["config"].image_config.aspect_ratio is None


def test_generate_image_no_image_retries_then_raises(client):
    client.models.responses = [text_response("I can't"), text_response("no")]
    with pytest.raises(GenerationError, match="no image part"):
        gen.generate_image(["x"])
    assert len(client.models.calls) == 2


def test_generate_image_no_image_then_image(client):
    data = png(4, 4)
    client.models.responses = [empty_response(), image_response(data)]
    assert gen.generate_image(["x"]) == data


def test_invalid_image_aspect_ratio_raises_before_call(client):
    with pytest.raises(InputError):
        gen.generate_image(["x"], params=ImageParams(aspect_ratio="7:3"))
    assert client.models.calls == []


# ---------------------------------------------------------------------- video


def _interaction(video=b"MP4DATA", *, uri=None, where="steps"):
    part = (
        {"type": "video", "uri": uri}
        if uri
        else {
            "type": "video",
            "data": base64.b64encode(video).decode(),
        }
    )
    if where == "steps":
        return {
            "steps": [
                {
                    "type": "thought",
                    "content": [{"type": "text", "text": "hmm"}],
                },
                {"type": "model_output", "content": [part]},
            ]
        }
    return {"outputs": [part]}


def test_generate_video_request(client, monkeypatch):
    monkeypatch.setenv("GENMEDIA_VIDEO_API_REVISION", "rev-1")
    get_models.cache_clear()
    client.interactions.responses = [_interaction(b"VIDEO")]
    exact = png(720, 1280)  # already the 9:16 box: sent as it is
    params = VideoParams(model="m-vid", aspect_ratio="9:16", resolution="720p")
    out = gen.generate_video([exact, png()], "spin it", params=params)
    assert out == b"VIDEO"
    assert client.get_client_calls == [
        {"location": "global", "api_revision": "rev-1"}
    ]
    call = client.interactions.calls[0]
    assert call["model"] == "m-vid"
    assert call["response_format"] == {
        "type": "video",
        "aspect_ratio": "9:16",
        "resolution": "720p",
    }
    assert call["input"][0] == {"type": "text", "text": "spin it"}
    assert call["input"][1]["mime_type"] == "image/png"
    assert base64.b64decode(call["input"][1]["data"]) == exact
    assert call["input"][2]["mime_type"] == "image/png"  # padded, lossless
    assert 0 < call["timeout"] <= VideoParams().deadline_s == 240


def test_a_stored_turn_returns_its_id_and_a_continuation_extends_it(client):
    first, whole = _interaction(b"HALF"), _interaction(b"WHOLE")
    first["id"], whole["id"] = "turn-1", "turn-2"
    client.interactions.responses = [first, whole]
    params = VideoParams(duration="4s")

    half = gen.generate_video([png()], "to the back", params=params, store=True)
    out = gen.generate_video(
        [png()], "to the front", params=params, extends=half.interaction_id
    )

    assert isinstance(half, StoredVideo) and half == b"HALF"
    assert (half.interaction_id, half.extends) == ("turn-1", None)
    assert out == b"WHOLE" and (out.interaction_id, out.extends) == (
        "turn-2",
        "turn-1",
    )
    one, two = client.interactions.calls
    assert one["store"] is True and "previous_interaction_id" not in one
    assert two["store"] is True and two["previous_interaction_id"] == "turn-1"
    assert one["input"][0]["text"] == "to the back"
    assert two["input"][0]["text"] == "to the front"
    assert one["response_format"]["duration"] == "4s"


def test_a_single_turn_video_is_not_stored(client):
    client.interactions.responses = [_interaction(b"V")]
    out = gen.generate_video([png()], "p")
    call = client.interactions.calls[0]
    assert "store" not in call and "duration" not in call["response_format"]
    assert type(out) is bytes


def test_generate_video_from_outputs_and_default_model(client):
    client.interactions.responses = [_interaction(b"V2", where="outputs")]
    assert gen.generate_video([png()], "p") == b"V2"
    assert client.interactions.calls[0]["model"] == get_models().video


def test_generate_video_uri_is_not_retryable(client):
    client.interactions.responses = [_interaction(uri="gs://b/v.mp4")]
    with pytest.raises(GenerationError) as info:
        gen.generate_video([png()], "p")
    assert info.value.retryable is False
    assert len(client.interactions.calls) == 1


def test_generate_video_no_video_retried_once(client):
    client.interactions.responses = [{"steps": []}, {"steps": []}]
    with pytest.raises(GenerationError, match="no video"):
        gen.generate_video([png()], "p")
    assert len(client.interactions.calls) == 2


def test_invalid_video_aspect_ratio(client):
    with pytest.raises(InputError):
        gen.generate_video([png()], "p", params=VideoParams(aspect_ratio="3:4"))
    assert client.interactions.calls == []


# ---------------------------------------------------------------- retry policy


def test_transport_errors_then_success(client, sleeps):
    client.models.responses = [
        ConnectionError("Connection aborted."),
        RuntimeError("EADDRNOTAVAIL"),
        text_response("ok"),
    ]
    assert gen.generate_text(["q"]) == "ok"
    assert len(client.models.calls) == 3
    assert sleeps == [1, 2]


def test_transport_errors_exhausted(client, sleeps):
    client.models.responses = [ConnectionError("reset")] * 4
    with pytest.raises(GenerationError) as info:
        gen.generate_text(["q"])
    assert info.value.retryable is True
    assert len(client.models.calls) == 4
    assert sleeps == [1, 2, 4]


def test_non_transient_error_not_retried_and_names_model(client):
    client.models.responses = [ValueError("404 model not found")]
    with pytest.raises(GenerationError, match="bad-model failed") as info:
        gen.generate_text(["q"], params=TextParams(model="bad-model"))
    assert info.value.retryable is False
    assert len(client.models.calls) == 1


def test_sdk_429_after_retries_is_retryable(client):
    exc = genai_errors.ClientError(429, {"error": {"message": "quota"}})
    client.models.responses = [exc]
    with pytest.raises(GenerationError) as info:
        gen.generate_text(["q"])
    assert info.value.retryable is True


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_deadline_expiry_mid_retry(client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(retry.time, "monotonic", clock)

    def slow_sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(retry.time, "sleep", slow_sleep)
    client.models.responses = [ConnectionError("reset")] * 4
    with pytest.raises(DeadlineExceeded):
        gen.generate_text(["q"], params=TextParams(deadline_s=2.5))
    # attempt 1 fails, sleep 1s, attempt 2 fails, 2s backoff >= 1.5s left;
    # then the same again with a fresh deadline (a stalled call)
    assert len(client.models.calls) == 4


def test_deadline_already_expired(client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(retry.time, "monotonic", clock)

    def slow_call(**kwargs):
        clock.now += 10
        raise ConnectionError("reset")

    client.models.generate_content = slow_call
    with pytest.raises(DeadlineExceeded):
        gen.generate_text(["q"], params=TextParams(deadline_s=5))


def _retrying_variation(client, policy):
    """A runner whose one variation makes a text call that keeps failing.

    Returns (events, finished): ``finished`` is set when the call's thread
    is done. Each failure is followed by a 1, 2, 4 s backoff.
    """
    client.models.responses = [ConnectionError("reset")] * 4
    finished = threading.Event()

    def call():
        try:
            return gen.generate_text(["q"])
        finally:
            finished.set()

    async def make(_index):
        return await asyncio.to_thread(call)

    async def evaluate(output):
        return Verdict(True), output

    return run_variations(policy, make, evaluate), finished


def test_cancelled_variation_stops_retrying(client):
    async def main():
        events, finished = _retrying_variation(client, RunPolicy(1))
        consumer = asyncio.create_task(anext(events))
        await asyncio.sleep(0.2)  # first attempt failed, 1 s backoff running
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        await events.aclose()
        return await asyncio.to_thread(finished.wait, 0.5)

    assert asyncio.run(main()) is True  # no longer sleeping or retrying
    assert len(client.models.calls) == 1


def test_timed_out_variation_stops_retrying(client):
    async def main():
        policy = RunPolicy(num_variations=1, deadline_s=0.2)
        events, finished = _retrying_variation(client, policy)
        variations = [e async for e in events if e.type == "variation"]
        return variations, await asyncio.to_thread(finished.wait, 0.5)

    (variation,), finished = asyncio.run(main())
    assert variation.status == "error"
    assert "timed out" in variation.error
    assert finished is True
    assert len(client.models.calls) == 1


def test_deadline_sleep_wakes_on_cancel():
    cancel = threading.Event()
    deadline = retry.Deadline(60, cancel=cancel)
    threading.Timer(0.05, cancel.set).start()
    assert deadline.sleep(5) is False
    assert deadline.cancelled
    with pytest.raises(DeadlineExceeded, match="cancelled"):
        deadline.check()


def test_keyboard_interrupt_not_caught(client):
    client.models.responses = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        gen.generate_text(["q"])


# ---------------------------------------------------------------------- embed


def _embedding(values):
    return types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=values)]
    )


def test_embed_one_call_per_item(client):
    client.models.responses = [_embedding([1.0, 0.0]), _embedding([0.0, 1.0])]
    out = gen.embed(
        ["a", png()], model="emb", location="us-central1", output_dims=2
    )
    assert out.shape == (2, 2)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, [[1, 0], [0, 1]])
    assert client.get_client_calls == [{"location": "us-central1"}]
    call = client.models.calls[0]
    assert call["model"] == "emb"
    assert call["config"].output_dimensionality == 2
    assert (
        client.models.calls[1]["contents"][0].parts[0].inline_data is not None
    )


def test_embed_empty_result_retried(client):
    client.models.responses = [
        types.EmbedContentResponse(embeddings=[]),
        _embedding([0.5]),
    ]
    out = gen.embed(["a"], model="emb")
    assert out.shape == (1, 1)
    config = client.models.calls[0]["config"]
    assert config.output_dimensionality is None
    # Each call carries the time left of the embed deadline as its timeout.
    assert 1000 < config.http_options.timeout <= gen.EMBED_DEADLINE_S * 1000


def test_embed_requires_items(client):
    with pytest.raises(InputError):
        gen.embed([], model="emb")


def test_embed_sequence_item_is_one_multipart_content(client):
    client.models.responses = [_embedding([1.0, 2.0]), _embedding([3.0, 4.0])]
    image = png()
    out = gen.embed([image, ["describe the view", image]], model="emb")
    assert out.shape == (2, 2)
    single, multi = (c["contents"] for c in client.models.calls)
    assert len(single) == 1 and len(single[0].parts) == 1
    assert len(multi) == 1
    assert [p.text for p in multi[0].parts] == ["describe the view", None]
    assert multi[0].parts[1].inline_data.mime_type == "image/png"


def test_embed_text_item_is_not_split(client):
    client.models.responses = [_embedding([1.0])]
    gen.embed(["abc"], model="emb")
    assert [p.text for p in client.models.calls[0]["contents"][0].parts] == [
        "abc"
    ]


# ---------------------------------------------------------------- video parts


def test_video_part_with_metadata(client):
    client.models.responses = [text_response('{"ok": true}')]
    video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
    assert gen.generate_json([(video, {"fps": 2}), "check"]) == {"ok": True}
    part = client.models.calls[0]["contents"][0].parts[0]
    assert part.inline_data.mime_type == "video/mp4"
    assert part.inline_data.data == video
    assert part.video_metadata.fps == 2


def test_unsupported_part_raises_input_error(client):
    with pytest.raises(InputError):
        gen.generate_text([("text", "not a video")])
    assert client.models.calls == []


def test_tuned_endpoint_is_called_in_its_region(client):
    client.models.responses = [text_response("right")]
    endpoint = "projects/123/locations/europe-west4/endpoints/456"
    gen.generate_text(["q"], params=TextParams(model=endpoint, thinking="OFF"))
    assert client.get_client_calls == [{"location": "europe-west4"}]
    assert client.models.calls[0]["model"] == endpoint


def test_plain_model_uses_the_default_location(client):
    client.models.responses = [text_response("x")]
    gen.generate_text(["q"], params=TextParams(model="gemini-x"))
    assert client.get_client_calls == [{"location": None}]


def test_an_image_attempt_that_outlives_its_deadline_is_asked_again(
    monkeypatch,
):
    """The SDK call hangs while keeping the connection alive: we stop
    waiting and ask again with a fresh deadline."""
    release, calls, data = threading.Event(), [], png(4, 4)

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                release.wait(10)  # no answer
            return image_response(data)

    class Client:
        models = Models()

    monkeypatch.setattr(gen.clients, "get_client", lambda **kw: Client())
    try:
        out = gen.generate_image([png()], params=ImageParams(deadline_s=0.1))
    finally:
        release.set()
    assert out == data and len(calls) == 2


# ---------------------------------------------------------------- stall retry


class Stalls:
    """A model call that gets no answer ``n`` times, then ``answer``.

    A stall moves the clock past the deadline and the HTTP read timeout
    fires, like a request the model never answers.
    """

    def __init__(self, clock, n, answer):
        self.clock, self.n, self.answer = clock, n, answer
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.n:
            self.clock.now += 1000
            raise TimeoutError("The read operation timed out")
        return self.answer


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(retry.time, "monotonic", fake)
    return fake


def test_a_stalled_call_is_asked_again_with_a_fresh_deadline(
    client, clock, caplog
):
    stalls = Stalls(clock, 1, text_response("ok"))
    client.models.generate_content = stalls
    params = TextParams(model="m-text", deadline_s=5)
    assert gen.generate_text(["q"], params=params) == "ok"
    _, second = stalls.calls
    assert second["config"].http_options.timeout == 5000  # all 5 s again
    assert "m-text: no answer within 5s, asked again (1/1)" in caplog.text


def test_a_call_that_stalls_twice_raises(client, clock):
    stalls = Stalls(clock, 2, text_response("late"))
    client.models.generate_content = stalls
    with pytest.raises(DeadlineExceeded):
        gen.generate_json(["q"], params=TextParams(deadline_s=5))
    assert len(stalls.calls) == 1 + gen.STALL_RETRIES == 2


def test_a_cancelled_call_is_not_asked_again(client, clock, caplog):
    cancel = threading.Event()
    stalls = Stalls(clock, 1, text_response("ok"))

    def cancelled(**kwargs):
        cancel.set()  # the run gave up on the variation meanwhile
        return stalls(**kwargs)

    client.models.generate_content = cancelled
    token = retry.CANCEL.set(cancel)
    try:
        with pytest.raises(DeadlineExceeded):
            gen.generate_text(["q"], params=TextParams(deadline_s=5))
    finally:
        retry.CANCEL.reset(token)
    assert len(stalls.calls) == 1
    assert "asked again" not in caplog.text


def test_a_stalled_video_call_is_asked_again(client, clock):
    stalls = Stalls(clock, 1, _interaction(b"VIDEO"))
    client.interactions.create = stalls
    params = VideoParams(deadline_s=60)
    assert gen.generate_video([png()], "p", params=params) == b"VIDEO"
    assert [c["timeout"] for c in stalls.calls] == [60, 60]


def test_embeddings_are_not_asked_again_on_a_stall(client, clock):
    stalls = Stalls(clock, 1, _embedding([1.0]))
    client.models.embed_content = stalls
    with pytest.raises(DeadlineExceeded):
        gen.embed(["a"], model="emb")
    assert len(stalls.calls) == 1
