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

"""Test helpers: synthetic media and the ``fake_gen`` fixture (dev only)."""

import copy
import inspect
import io
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import gen
from genmedia4commerce.core.media import sniff_mime

__all__ = ["FakeGen", "fake_gen", "mp4", "mp4_stub", "png"]

_FUNCTIONS = {
    "text": "generate_text",
    "json": "generate_json",
    "image": "generate_image",
    "video": "generate_video",
    "embed": "embed",
}


def png(
    w: int = 64, h: int = 64, color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    """Return a solid-colour PNG."""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def mp4(frames: Sequence[np.ndarray], *, fps: float = 12) -> bytes:
    """Encode RGB frames (same size) as an MP4 with OpenCV's MPEG-4 encoder."""
    h, w = frames[0].shape[:2]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
        if not writer.isOpened():
            raise RuntimeError("OpenCV cannot write an MP4")
        for frame in frames:
            writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        writer.release()
        return path.read_bytes()


def mp4_stub(frames: int = 12, *, size: int = 64, fps: int = 12) -> bytes:
    """Return a tiny valid MP4 whose frames get brighter one by one."""
    values = [int(255 * i / max(frames - 1, 1)) for i in range(frames)]
    return mp4([np.full((size, size, 3), v, np.uint8) for v in values], fps=fps)


def _summary(parts: Any) -> list:
    return [
        f"<{sniff_mime(p)} {len(p)} bytes>" if isinstance(p, bytes) else p
        for p in parts
    ]


class FakeGen:
    """Replaces ``core.gen`` calls with canned responders and records calls.

    A responder is a value, an exception instance (raised), or a callable
    receiving the same arguments as the real function. Each setter replaces
    the previous responders; passing a ``list`` queues them: each call pops
    the next and the last one repeats (wrap a top-level JSON list response in
    a list, or use a callable). An unset kind raises ``AssertionError``.

    ``calls`` holds one dict per call: ``fn``, ``parts`` (bytes summarised as
    ``"<mime N bytes>"``), ``raw_parts`` and every other bound argument
    (``system``, ``params``, ``schema``, ``prompt``, ``model``...).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        """Patch the five ``core.gen`` functions."""
        self.calls: list[dict] = []
        self._responders: dict[str, list] = {kind: [] for kind in _FUNCTIONS}
        self._lock = threading.Lock()
        for kind, name in _FUNCTIONS.items():
            monkeypatch.setattr(gen, name, self._fake(kind, getattr(gen, name)))

    def _fake(self, kind: str, real: Callable) -> Callable:
        signature = inspect.signature(real)

        def fake(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            first = next(iter(arguments))
            raw = list(arguments.pop(first))
            call = {
                "fn": real.__name__,
                "parts": _summary(raw),
                "raw_parts": raw,
            }
            with self._lock:
                self.calls.append({**call, **arguments})
                queue = self._responders[kind]
                if not queue:
                    raise AssertionError(f"fake_gen: no {kind} responder set")
                responder = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(responder, BaseException):
                raise responder
            if callable(responder):
                return responder(*args, **kwargs)
            return copy.deepcopy(responder)

        return fake

    def _set(self, kind: str, value: Any) -> None:
        with self._lock:
            self._responders[kind] = (
                list(value) if isinstance(value, list) else [value]
            )

    def text(self, value: str | Callable[..., str]) -> None:
        """Set the ``generate_text`` responder(s)."""
        self._set("text", value)

    def json(self, value: dict | list | Callable[..., dict | list]) -> None:
        """Set the ``generate_json`` responder(s); a list is a queue."""
        self._set("json", value)

    def image(self, value: bytes | Callable[..., bytes]) -> None:
        """Set the ``generate_image`` responder(s)."""
        self._set("image", value)

    def video(self, value: bytes | Callable[..., bytes]) -> None:
        """Set the ``generate_video`` responder(s)."""
        self._set("video", value)

    def embed(self, value: np.ndarray | Callable[..., np.ndarray]) -> None:
        """Set the ``embed`` responder(s)."""
        self._set("embed", value)


@pytest.fixture
def fake_gen(monkeypatch: pytest.MonkeyPatch) -> FakeGen:
    """Fake every ``core.gen`` call for the duration of a test."""
    return FakeGen(monkeypatch)
