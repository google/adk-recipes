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

"""In-container runtime shim (``app/sandbox/runtime/server.py``) tests.

The shim is normally launched inside the BYOC container with
``--app-dir /opt/runtime``; here we put that dir on ``sys.path`` and point
``SANDBOX_WORKSPACE`` at a tmp dir so the FastAPI app can be driven with
``TestClient`` against the host filesystem.
"""

from __future__ import annotations

import importlib
import io
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

RUNTIME_DIR = (
    Path(__file__).resolve().parents[2] / "app" / "sandbox" / "runtime"
)


@pytest.fixture
def shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SANDBOX_WORKSPACE", str(tmp_path))
    monkeypatch.syspath_prepend(str(RUNTIME_DIR))
    # WORKSPACE_ROOT is resolved at import time — drop any cached module so
    # the fresh import picks up the tmp workspace.
    sys.modules.pop("server", None)
    sys.modules.pop("protocol", None)
    server = importlib.import_module("server")
    return TestClient(server.app), tmp_path


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in entries.items():
            zf.writestr(arcname, data)
    return buf.getvalue()


class TestUploadZip:
    def test_extracts_flat_and_nested_files(self, shim: Any) -> None:
        client, workspace = shim
        payload = _zip_bytes({"a.txt": b"alpha", "sub/dir/b.txt": b"beta"})

        r = client.post(
            "/files/zip", params={"path": str(workspace)}, content=payload
        )

        assert r.status_code == 200
        assert (workspace / "a.txt").read_bytes() == b"alpha"
        assert (workspace / "sub" / "dir" / "b.txt").read_bytes() == b"beta"

    def test_extracts_into_subdirectory(self, shim: Any) -> None:
        client, workspace = shim
        payload = _zip_bytes({"keep.txt": b"hi"})

        r = client.post(
            "/files/zip",
            params={"path": str(workspace / "restored")},
            content=payload,
        )

        assert r.status_code == 200
        assert (workspace / "restored" / "keep.txt").read_bytes() == b"hi"

    def test_rejects_zip_slip(self, shim: Any) -> None:
        client, workspace = shim
        payload = _zip_bytes({"../escape.txt": b"pwned"})

        r = client.post(
            "/files/zip", params={"path": str(workspace)}, content=payload
        )

        assert r.status_code == 403
        assert not (workspace.parent / "escape.txt").exists()

    def test_rejects_invalid_zip(self, shim: Any) -> None:
        client, workspace = shim

        r = client.post(
            "/files/zip", params={"path": str(workspace)}, content=b"not a zip"
        )

        assert r.status_code == 400

    def test_round_trips_with_download_zip(self, shim: Any) -> None:
        """Download from one path, upload into another — the migration path."""
        client, workspace = shim
        (workspace / "src").mkdir()
        (workspace / "src" / "f.txt").write_bytes(b"payload")

        dl = client.get("/files/zip", params={"path": str(workspace / "src")})
        assert dl.status_code == 200

        up = client.post(
            "/files/zip",
            params={"path": str(workspace / "dst")},
            content=dl.content,
        )
        assert up.status_code == 200
        assert (workspace / "dst" / "f.txt").read_bytes() == b"payload"
