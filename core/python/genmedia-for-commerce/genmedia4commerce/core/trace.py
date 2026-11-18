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

"""Debug trace: every intermediate result of a run, on local disk.

Off unless ``GENMEDIA_DEBUG_DIR`` is set (``settings.debug_dir``). ``start``
opens ``<debug_dir>/<YYYY-MM-DD>/<run_id>/`` with a ``run.json`` and sets
``TRACE``. ``scope`` (code) and ``scoped`` (event streams) add labels:
``variation``, ``attempt``, ``stage``, ``view``. ``save`` writes one file,

    {seq:03d}_{HH-MM-SS.mmm}_v{variation}a{attempt}_{stage}_{view}_{label}.{ext}

(labels not set are left out; times are UTC), and appends one line to
``trace.jsonl``. ``call`` records a model call for ``core.gen``. Like
``core.retry.CANCEL`` the trace is a ContextVar, so new tasks,
``asyncio.to_thread`` and ``ContextPool`` carry it. Nothing here raises: the
first write error is logged and ends that run's trace.
"""

import contextlib
import dataclasses
import datetime
import itertools
import json
import logging
import re
import threading
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, TypeVar

from genmedia4commerce.core import events, media
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import get_settings

__all__ = [
    "NO_CALL",
    "TRACE",
    "Trace",
    "call",
    "save",
    "scope",
    "scoped",
    "start",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_RUN_SETTINGS = (
    "location",
    "region",
    "run_mode",
    "num_variations",
    "max_attempts",
    "videos_per_image",
    "input_mode",
    "output_mode",
    "use_cache",
)


@dataclasses.dataclass(frozen=True, eq=False)
class Trace:
    """A run's trace folder and the labels of the current scope.

    Children (``child``) share the numbering, the lock and the broken flag.
    """

    root: Path
    labels: dict[str, str | int] = dataclasses.field(default_factory=dict)
    count: Iterator[int] = dataclasses.field(
        default_factory=lambda: itertools.count(1)
    )
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    broken: threading.Event = dataclasses.field(default_factory=threading.Event)

    def child(self, **labels: str | int) -> "Trace":
        """This trace with ``labels`` added (or replaced)."""
        return dataclasses.replace(self, labels={**self.labels, **labels})


TRACE: ContextVar[Trace | None] = ContextVar("trace", default=None)


def _describe(data: bytes) -> str:
    return f"<{media.sniff_mime(data)} {len(data)} bytes>"


def _default(value: Any) -> Any:
    """JSON for SDK configs (pydantic), numpy values and anything else."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


def _dumps(value: Any, indent: int | None = 2) -> str:
    """JSON with bytes summarised (never inlined) and unknown types as text."""
    return json.dumps(
        events._jsonable(value, _describe),
        indent=indent,
        default=_default,
        skipkeys=True,
    )


def _encode(data: Any) -> tuple[bytes | None, str]:
    """File content and extension: media as is, text as txt, else JSON."""
    if data is None:
        return None, ""
    if isinstance(data, bytes):
        return data, media.EXTENSIONS.get(media.sniff_mime(data), "bin")
    if isinstance(data, str):
        return data.encode(), "txt"
    return _dumps(data).encode(), "json"


def _name(
    seq: int, now: datetime.datetime, labels: dict, label: str, ext: str
) -> str:
    v, a = labels.get("variation"), labels.get("attempt")
    tag = ("" if v is None else f"v{v}") + ("" if a is None else f"a{a}")
    names = (tag, labels.get("stage"), labels.get("view"), label)
    safe = [_UNSAFE.sub("-", str(n)) for n in names if n not in (None, "")]
    clock = now.strftime("%H-%M-%S.") + f"{now.microsecond // 1000:03d}"
    return "_".join([f"{seq:03d}", clock, *safe]) + f".{ext}"


def _write(
    tr: Trace, label: str, data: Any, meta: dict | None
) -> tuple[int, Path | None]:
    """Write ``data`` (None: no file) and its jsonl line: (seq, file).

    (0, None) once the trace is broken.
    """
    if tr.broken.is_set():
        return 0, None
    try:
        body, ext = _encode(data)
        with tr.lock:
            seq = next(tr.count)
            now = datetime.datetime.now(datetime.UTC)
            name = None
            if body is not None:
                name = _name(seq, now, tr.labels, label, ext)
                (tr.root / name).write_bytes(body)
            line = {
                "seq": seq,
                "ts": now.isoformat(timespec="milliseconds"),
                "labels": tr.labels,
                "label": label,
                "file": name,
                "meta": meta,
            }
            with (tr.root / "trace.jsonl").open("a", encoding="utf-8") as f:
                f.write(_dumps(line, indent=None) + "\n")
    except Exception as exc:
        if not tr.broken.is_set():
            tr.broken.set()
            logger.warning(f"debug trace stopped ({tr.root}): {exc}")
        return 0, None
    return seq, tr.root / name if name else None


def start(run_id: str, *, workflow: str, session_id: str) -> Token | None:
    """Open the run's trace folder and set ``TRACE``; None when tracing is off.

    Returns the token for ``TRACE.reset``. A folder that cannot be created
    is logged and leaves tracing off for this run.
    """
    settings = get_settings()
    if settings.debug_dir is None:
        return None
    now = datetime.datetime.now(datetime.UTC)
    root = settings.debug_dir / now.date().isoformat() / run_id
    info = {
        "run_id": run_id,
        "session_id": session_id,
        "workflow": workflow,
        "started_at": now.isoformat(timespec="milliseconds"),
        "models": get_models(),
        "settings": {name: getattr(settings, name) for name in _RUN_SETTINGS},
    }
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "run.json").write_text(_dumps(info), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"debug trace off for run {run_id}: {exc}")
        return None
    return TRACE.set(Trace(root))


def save(label: str, data: Any, *, meta: dict | None = None) -> Path | None:
    """Save ``data`` in the current trace; the file, or None.

    ``data``: bytes (png/jpg/webp/mp4 by signature, else bin), str (txt),
    or a JSON-able value or dataclass (json, bytes summarised). ``None``
    writes only the ``trace.jsonl`` line.
    """
    tr = TRACE.get()
    return None if tr is None else _write(tr, label, data, meta)[1]


@contextlib.contextmanager
def scope(**labels: str | int) -> Iterator[None]:
    """Run the block in the current trace with ``labels`` added.

    Not across a ``yield`` of an async generator: use ``scoped`` there.
    """
    current = TRACE.get()
    if current is None:
        yield
        return
    token = TRACE.set(current.child(**labels))
    try:
        yield
    finally:
        TRACE.reset(token)


def scoped(
    stream: AsyncGenerator[T, None], **labels: str | int
) -> AsyncGenerator[T, None]:
    """``stream`` with each step run in the current trace plus ``labels``.

    The trace is set only while ``stream`` computes its next item, so it
    reaches the tasks and threads the stream starts but never leaks into
    the consumer between items, whichever task iterates.
    """
    current = TRACE.get()
    if current is None:
        return stream
    return _steps(stream, current.child(**labels))


async def _steps(
    stream: AsyncGenerator[T, None], tr: Trace
) -> AsyncGenerator[T, None]:
    async with contextlib.aclosing(stream):
        while True:
            token = TRACE.set(tr)
            try:
                item = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                TRACE.reset(token)
            yield item


class _NoCall:
    """What ``call`` yields with no trace: takes everything, saves nothing."""

    def __setattr__(self, name: str, value: Any) -> None:
        pass

    def done(self, result: Any, *, duration_ms: int) -> None:
        """Do nothing."""

    def failed(self, exc: BaseException) -> None:
        """Do nothing."""


NO_CALL = _NoCall()


class _Call:
    """One model call being recorded; ``retry`` and ``finish`` set by gen."""

    def __init__(self, tr: Trace, call_id: int, model: str):
        self._tr = tr
        self._meta = {"call": call_id, "model": model}
        self.retry = 0
        self.finish = None

    def _attempt(self) -> dict:
        meta = {**self._meta, "retry": self.retry, "finish": self.finish}
        self.finish = None
        return meta

    def done(self, result: Any, *, duration_ms: int) -> None:
        """Save the attempt's result as ``response.<ext>``."""
        meta = {**self._attempt(), "duration_ms": duration_ms}
        _write(self._tr, "response", result, meta)

    def failed(self, exc: BaseException) -> None:
        """Save the attempt's error as ``response-error.txt``, with the
        whole answer when it was unreadable (the message has its start)."""
        error = f"{type(exc).__name__}: {exc}"
        if text := getattr(exc, "text", ""):
            error += f"\n\nFULL ANSWER:\n{text}"
        _write(self._tr, "response-error", error, self._attempt())


def _pixels(data: bytes) -> list[int] | None:
    """[width, height] of an image part as sent; None for other media."""
    if not media.sniff_mime(data).startswith("image/"):
        return None
    try:
        return list(media.image_size(data))
    except InputError:
        return None


def _request(
    tr: Trace, model: str, parts: Sequence, system: str | None, params: Any
) -> _Call:
    """Save ``request.txt`` (system and parts in order) and each media part.

    Each image part's line carries its size as sent (``sent_px``).
    """
    lines = [] if system is None else ["--- SYSTEM ---", str(system)]
    media_parts = []
    for n, part in enumerate(parts):
        data, extra = part if isinstance(part, tuple) else (part, None)
        lines.append(f"--- PART {n} ---")
        if isinstance(data, bytes):
            lines.append(f"{_describe(data)} request-input-{n}")
            media_parts.append((n, data, extra))
        else:
            lines.append(str(data))
    meta = {"model": model, "params": params}
    call_id, _ = _write(tr, "request", "\n".join(lines) + "\n", meta)
    for n, data, extra in media_parts:
        meta = {"call": call_id, "metadata": extra, "sent_px": _pixels(data)}
        _write(tr, f"request-input-{n}", data, meta)
    return _Call(tr, call_id, model)


@contextlib.contextmanager
def call(
    model: str,
    parts: Sequence,
    *,
    system: str | None = None,
    params: Any = None,
) -> Iterator[_Call | _NoCall]:
    """Record one model call: the request now, each attempt via the record.

    Yields ``NO_CALL`` with no trace. Responses and errors carry ``call``,
    the seq of their request line.
    """
    tr = TRACE.get()
    if tr is None:
        yield NO_CALL
        return
    yield _request(tr, model, parts, system, params)
