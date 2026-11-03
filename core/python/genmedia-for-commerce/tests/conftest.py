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

"""Shared pytest fixtures for GenMedia tests."""

import contextlib
import io
import json
import os
import re
import threading
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed
from PIL import Image

from genmedia4commerce.core import clients, gcs, models, settings, trace
from genmedia4commerce.core import testing as core_testing

# Some test modules build gs:// paths from the settings at import, before
# _hermetic_core sets PROJECT_ID: a default for runs without one (CI).
os.environ.setdefault("PROJECT_ID", "test-project")

# PNG magic bytes (minimal valid PNG header)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# JPEG magic bytes
JPEG_SIGNATURE = b"\xff\xd8\xff\xe0\x00\x10JFIF"

# WebP magic bytes (RIFF + size + WEBP)
WEBP_SIGNATURE = b"RIFF\x00\x00\x00\x00WEBP"

# GIF magic bytes
GIF_SIGNATURE = b"GIF89a"

# AVIF magic bytes (ftyp box with avif brand)
AVIF_SIGNATURE = b"\x00\x00\x00\x1cftypavifall"

# A real HEIC photo, 1280x960 in 512 px tiles like an iPhone's, left half red
# and right half blue. Encoded once with libheif and x265: the app only decodes
# HEIC (pi-heif-decoder), so the tests cannot make one.
HEIC = (Path(__file__).parent / "fixtures" / "photo.heic").read_bytes()


@pytest.fixture
def png_bytes():
    """Minimal valid PNG bytes with magic header."""
    return PNG_SIGNATURE + b"\x00" * 20  # Pad to meet minimum length


@pytest.fixture
def jpeg_bytes():
    """Minimal JPEG bytes with magic header."""
    return JPEG_SIGNATURE + b"\x00" * 20


@pytest.fixture
def webp_bytes():
    """Minimal WebP bytes with magic header."""
    return WEBP_SIGNATURE + b"\x00" * 20


@pytest.fixture
def gif_bytes():
    """Minimal GIF bytes with magic header."""
    return GIF_SIGNATURE + b"\x00" * 20


@pytest.fixture
def avif_bytes():
    """Minimal AVIF bytes with magic header."""
    return AVIF_SIGNATURE + b"\x00" * 20


@pytest.fixture
def unknown_bytes():
    """Unknown format bytes (no recognizable magic header)."""
    return b"\x00\x01\x02\x03" * 10


@pytest.fixture
def sample_image_bytes():
    """Create a simple 100x100 red image as PNG bytes."""
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def sample_image_bytes_200x100():
    """Create a 200x100 blue image as PNG bytes (landscape)."""
    img = Image.new("RGB", (200, 100), color=(0, 0, 255))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def sample_image_bytes_100x200():
    """Create a 100x200 green image as PNG bytes (portrait)."""
    img = Image.new("RGB", (100, 200), color=(0, 255, 0))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def sample_rgba_image_bytes():
    """Create a 100x100 RGBA image with transparency as PNG bytes."""
    img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


# `fake_gen` fixture (spec §3.10), available to every test.
fake_gen = core_testing.fake_gen


def _no_real_client(*_args, **_kwargs):
    raise AssertionError("real client in unit test")


@pytest.fixture(autouse=True)
def _hermetic_core(request, monkeypatch, tmp_path):
    """Hermetic defaults for every test (spec §0.2.11, §3.10).

    Unit tests get PROJECT_ID=test-project, no catalogue file (the ``catalog_file`` fixture writes one), inline
    input and output, no debug trace, no startup model warm-up, no preprocessing cache (``tests/workflows/tryon/test_cache.py`` turns it on over the ``storage`` fixture), the default videos per image and no try-on fix pass (whatever config.env says), fresh settings/models/client caches,
    and any real genai client construction fails. Tests marked ``live`` keep the real environment and are skipped
    unless GENMEDIA_LIVE=1.
    """
    live = request.node.get_closest_marker("live") is not None
    if live and os.getenv("GENMEDIA_LIVE") != "1":
        pytest.skip("live test: set GENMEDIA_LIVE=1 to run")
    if not live:
        monkeypatch.setenv("PROJECT_ID", "test-project")
        monkeypatch.setenv(
            "GENMEDIA_CATALOG_PATH",
            str(tmp_path / "catalog" / "products.parquet"),
        )
        monkeypatch.delenv("GENMEDIA_OUTPUT", raising=False)
        monkeypatch.delenv("GENMEDIA_INPUT", raising=False)
        monkeypatch.delenv("GENMEDIA_DEBUG_DIR", raising=False)
        monkeypatch.delenv("GENMEDIA_VIDEOS_PER_IMAGE", raising=False)
        monkeypatch.delenv("GENMEDIA_TRYON_FIX", raising=False)
        monkeypatch.setenv("GENMEDIA_USE_CACHE", "false")
        monkeypatch.setattr(clients, "get_client", _no_real_client)
    settings.get_settings.cache_clear()
    models.get_models.cache_clear()
    clients.reset_clients()
    yield
    settings.get_settings.cache_clear()
    models.get_models.cache_clear()
    clients.reset_clients()


async def _photos_as_they_are(req, _classification):
    return req


@pytest.fixture(autouse=True)
def _no_mannequin(request, monkeypatch):
    """Worn product photos stay as they are (``workflows.tryon.worn``): the
    image fakes of most tests answer every image call. The tests of the
    conversion ask for ``mannequin_on``; live tests keep it."""
    if request.node.get_closest_marker("live") is None:
        from genmedia4commerce.workflows.tryon import worn

        monkeypatch.setattr(worn, "normalise", _photos_as_they_are)


class FakeBlob:
    """The parts of ``storage.Blob`` that ``gcs.blob`` callers use."""

    def __init__(self, fake: "FakeStorage", uri: str):
        self.fake, self.uri = fake, uri
        self.generation: int | None = None

    def download_as_bytes(self) -> bytes:
        generation = self.fake.generations.get(self.uri)
        data = self.fake.download(self.uri)
        self.generation = generation
        return data

    def upload_from_string(
        self, data: bytes, content_type: str = "", if_generation_match=None
    ) -> None:
        del content_type
        self.fake.write(self.uri, data, if_generation_match)

    def delete(self) -> None:
        self.fake.delete(self.uri)


class FakeStorage:
    """``gcs.download`` / ``upload`` / ``blob`` / ``list_paths`` over a dict of
    ``gs://`` URIs, with object generations (``if_generation_match``).

    To make a call fail, replace ``download`` or ``write`` on the instance.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, bytes] = {}  # written by the code under test
        self.generations: dict[str, int] = {}
        self._lock = threading.Lock()

    def download(self, uri: str) -> bytes:
        if uri not in self.objects:
            raise NotFound(uri)
        return self.objects[uri]

    def write(
        self, uri: str, data: bytes, if_generation_match: int | None = None
    ) -> None:
        with self._lock:
            current = self.generations.get(uri, 0)
            if if_generation_match is not None and (
                if_generation_match != current
            ):
                raise PreconditionFailed(uri)
            self.objects[uri] = self.uploads[uri] = data
            self.generations[uri] = current + 1

    def upload(
        self, data: bytes, *, bucket: str, path: str, content_type: str
    ) -> str:
        uri = f"gs://{bucket}/{path}"
        self.write(uri, data)
        return uri

    def blob(self, uri: str) -> FakeBlob:
        return FakeBlob(self, uri)

    def delete(self, uri: str) -> None:
        with self._lock:
            if self.objects.pop(uri, None) is None:
                raise NotFound(uri)
            self.generations.pop(uri, None)

    def list_paths(
        self, bucket: str, prefix: str, match_glob: str | None = None
    ) -> list[str]:
        """Names under ``prefix``; in ``match_glob`` only ``*`` (not
        across ``/``) and ``**`` are supported."""
        pattern = (match_glob or prefix + "**").replace("**", "\0")
        regex = re.escape(pattern).replace("\\*", "[^/]*")
        regex = regex.replace("\0", ".*")
        with self._lock:
            names = [
                uri.removeprefix(f"gs://{bucket}/")
                for uri in self.objects
                if uri.startswith(f"gs://{bucket}/{prefix}")
            ]
        return sorted(n for n in names if re.fullmatch(regex, n))

    def media(self, path: str) -> str:
        """Full URI of ``path`` in the media bucket."""
        return f"gs://{settings.get_settings().media_bucket}/{path}"


@pytest.fixture
def storage(monkeypatch) -> FakeStorage:
    """An in-memory Cloud Storage behind ``gcs.download``, ``gcs.upload``,
    ``gcs.blob`` and ``gcs.list_paths``, with an empty session cache."""
    from genmedia4commerce.studio import sessions
    from genmedia4commerce.studio.session_cache import SessionCache

    monkeypatch.setattr(sessions, "_CACHE", SessionCache())
    fake = FakeStorage()
    monkeypatch.setattr(gcs, "download", fake.download)
    monkeypatch.setattr(gcs, "upload", fake.upload)
    monkeypatch.setattr(gcs, "blob", fake.blob)
    monkeypatch.setattr(gcs, "list_paths", fake.list_paths)
    return fake


TRACE_RUN_ID = "abcdef123456"


@pytest.fixture
def debug_dir(monkeypatch, tmp_path) -> Path:
    """Debug tracing on (``GENMEDIA_DEBUG_DIR``) into a temporary folder."""
    path = tmp_path / "debug"
    monkeypatch.setenv("GENMEDIA_DEBUG_DIR", str(path))
    settings.get_settings.cache_clear()
    return path


@contextlib.contextmanager
def tracing(run_id: str = TRACE_RUN_ID):
    """Trace the block as run ``run_id``; yields the run's trace folder."""
    token = trace.start(run_id, workflow="test", session_id="s")
    assert token is not None, "set GENMEDIA_DEBUG_DIR (debug_dir fixture)"
    current = trace.TRACE.get()
    assert current is not None
    try:
        yield current.root
    finally:
        trace.TRACE.reset(token)


def trace_lines(root: Path) -> list[dict]:
    """The parsed lines of a run's ``trace.jsonl``."""
    text = (root / "trace.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def trace_file(root: Path, suffix: str) -> Path:
    """The one file of the trace folder whose name ends with ``suffix``."""
    found = [p for p in root.iterdir() if p.name.endswith(suffix)]
    assert len(found) == 1, (suffix, sorted(p.name for p in root.iterdir()))
    return found[0]


def _webp(color: tuple[int, int, int], size: tuple[int, int] = (300, 400)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="WEBP")
    return buf.getvalue()


def _unit(i: int) -> list[float]:
    vector = [0.0] * 256
    vector[i] = 1.0
    return vector


#: The rows of ``catalog_file``: (id, kind, title, family, audience, color,
#: featured, number of images, embedding axis or None, source_image).
CATALOG_ROWS = (
    ("pa1", "product", "White cotton t-shirt", "tops", "women", "white",
     True, 1, 0, None),
    ("pb2", "product", "Black leather jacket", "outerwear", "men", "black",
     False, 1, 1, "gs://old-assets/b2.jpg"),
    ("pc3", "product", "Blue denim jeans", "bottoms", "unisex", "blue",
     False, 1, 2, None),
    ("p_glasses_4", "product", "Amber sunglasses", "bags_accessories",
     "unisex", None, True, 1, None, None),
    ("m_emma", "model", "Emma", None, "women", None, False, 1, None, None),
    ("s_shoe", "spin", "Red sneaker", "shoes", "unisex", "red", False, 3,
     None, None),
)  # fmt: skip


def write_catalog(path: Path, rows=CATALOG_ROWS) -> Path:
    """Write a tiny catalogue in the schema of ``core.catalog``."""
    records = []
    for i, (item_id, kind, title, family, audience, color, featured, n,
            axis, source) in enumerate(rows):  # fmt: skip
        records.append(
            {
                "id": item_id,
                "kind": kind,
                "title": title,
                "description": title
                if kind != "product"
                else (
                    f"{audience}'s {color or ''} {title.lower()}. A test item."
                ),
                "category": title.split()[-1].lower(),
                "family": family,
                "audience": audience,
                "color": color,
                "color_family": color,
                "season": "all seasons",
                "style": "casual",
                "featured": featured,
                "images": [_webp((40 * i, 80, 160 + j)) for j in range(n)],
                "embedding": _unit(axis) if axis is not None else None,
                "source_idx": None,
                "source_image": source,
            }
        )
    schema = pa.schema(
        [
            *[(k, pa.string()) for k in ("id", "kind", "title",
                                         "description", "category", "family",
                                         "audience", "color", "color_family",
                                         "season", "style")],
            ("featured", pa.bool_()),
            ("images", pa.list_(pa.binary())),
            ("embedding", pa.list_(pa.float16(), 256)),
            ("source_idx", pa.string()),
            ("source_image", pa.string()),
        ]
    )  # fmt: skip
    for r in records:
        if r["embedding"] is not None:
            r["embedding"] = np.asarray(r["embedding"], dtype=np.float16)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records, schema=schema), path)
    return path


@pytest.fixture
def catalog_file() -> Path:
    """The tiny catalogue of ``CATALOG_ROWS`` at ``settings.catalog_path``."""
    return write_catalog(settings.get_settings().catalog_path)
