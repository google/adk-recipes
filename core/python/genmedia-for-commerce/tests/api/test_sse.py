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

"""SSE framing, keepalive and cancellation on client disconnect."""

import asyncio
import io
import json

import pytest
from fastapi import HTTPException, UploadFile

from genmedia4commerce.api import sse
from genmedia4commerce.core.errors import (
    SERVER_FAILED,
    ConfigError,
    GenerationError,
    InputError,
    StorageError,
)
from genmedia4commerce.core.events import CompleteEvent, ProgressEvent

pytestmark = pytest.mark.asyncio


class FakeRequest:
    """Stands in for ``starlette.requests.Request``."""

    def __init__(self, disconnected: bool = False):
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


def slow_workflow(state: dict, *, pause: float = 60.0):
    """One progress event, then a long wait; records how it ended."""

    async def run():
        try:
            yield ProgressEvent(stage="started", message="Started")
            await asyncio.sleep(pause)
            yield CompleteEvent(ranked_indices=[], best_index=None)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        finally:
            state["closed"] = True

    return run()


async def test_frames_are_data_lines():
    async def run():
        yield ProgressEvent(stage="a", message="hi")
        yield CompleteEvent(ranked_indices=[0], best_index=0)

    chunks = [c async for c in sse._frames(run(), FakeRequest())]

    assert [json.loads(c.removeprefix("data: ")) for c in chunks] == [
        {"type": "progress", "stage": "a", "message": "hi", "data": {}},
        {"type": "complete", "ranked_indices": [0], "best_index": 0},
    ]
    assert all(c.startswith("data: ") and c.endswith("\n\n") for c in chunks)


async def test_keepalive_while_waiting(monkeypatch):
    monkeypatch.setattr(sse, "KEEPALIVE_S", 0.01)
    state: dict = {}
    frames = sse._frames(slow_workflow(state, pause=0.05), FakeRequest())

    chunks = [c async for c in frames]

    assert chunks[0].startswith("data: ")
    assert ": keepalive\n\n" in chunks
    assert chunks[-1].startswith("data: ") and "complete" in chunks[-1]


async def test_consumer_close_cancels_workflow():
    state: dict = {}
    frames = sse._frames(slow_workflow(state), FakeRequest())

    first = await anext(frames)
    await frames.aclose()

    assert "started" in first
    assert state == {"cancelled": True, "closed": True}


async def test_disconnect_detected_on_keepalive_cancels_workflow(monkeypatch):
    monkeypatch.setattr(sse, "KEEPALIVE_S", 0.01)
    state: dict = {}
    request = FakeRequest()
    frames = sse._frames(slow_workflow(state), request)

    await anext(frames)
    request.disconnected = True
    rest = [c async for c in frames]

    assert rest == []
    assert state == {"cancelled": True, "closed": True}


async def test_unexpected_exception_becomes_a_generic_error_event(caplog):
    async def run():
        yield ProgressEvent(stage="a", message="A")
        raise ValueError("gs://secret-bucket/key.json: 403 for sa@x.iam")

    chunks = [c async for c in sse._frames(run(), FakeRequest())]

    assert json.loads(chunks[-1].removeprefix("data: ")) == {
        "type": "error",
        "message": SERVER_FAILED,
        "retryable": False,
    }
    assert "secret-bucket" not in "".join(chunks)
    assert "secret-bucket" in caplog.text  # logged with its traceback


@pytest.mark.parametrize(
    ("error", "message", "retryable"),
    [
        (InputError("bad motion"), "bad motion", False),
        (GenerationError("quota", retryable=True), "quota", True),
        (StorageError("could not read gs://b/x"), SERVER_FAILED, False),
        (ConfigError("PROJECT_ID is not set"), SERVER_FAILED, False),
    ],
)
async def test_only_user_facing_errors_keep_their_message(
    error, message, retryable
):
    async def run():
        yield ProgressEvent(stage="a", message="A")
        raise error

    chunks = [c async for c in sse._frames(run(), FakeRequest())]

    assert json.loads(chunks[-1].removeprefix("data: ")) == {
        "type": "error",
        "message": message,
        "retryable": retryable,
    }


class CountingUpload(UploadFile):
    """An upload that records how much each read asked for."""

    def __init__(self, data: bytes):
        super().__init__(io.BytesIO(data), filename="big.png")
        self.asked: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.asked.append(size)
        return await super().read(size)


async def test_an_upload_is_read_only_up_to_one_byte_over_the_limit(
    monkeypatch,
):
    monkeypatch.setattr(sse, "MAX_IMAGE_BYTES", 10)
    upload = CountingUpload(b"\x89PNG\r\n\x1a\n" + b"x" * 1000)

    with pytest.raises(HTTPException) as caught:
        await sse.read_images("file", [upload])

    assert caught.value.status_code == 400
    assert "at most" in caught.value.detail
    assert upload.asked == [11]
