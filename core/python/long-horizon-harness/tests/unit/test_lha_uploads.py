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

"""Unit tests for ``POST /lha/uploads`` and ``GET /lha/workspace/tree``.

The endpoints resolve the user's environment via
``session_start._ensure_environment``; tests monkeypatch that to hand
back a ``LocalEnvironment`` rooted at ``tmp_path``. The tree handler
dispatches by env type — ``LocalEnvironment`` exercises the
in-process ``os.scandir`` branch; the ``SandboxEnvironment`` branch is
covered separately via the shim's own unit tests.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from horizon.api.uploads import (
    MAX_TREE_ENTRIES,
    attach_uploads_routes,
)
from horizon.auth import current_user_id
from horizon.conversation import session_start
from horizon.environment import LocalEnvironment

USER_ID = "u@local"


def _build_app() -> FastAPI:
    app = FastAPI()
    attach_uploads_routes(app)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return app


@pytest.fixture
def env_at_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalEnvironment:
    """Patch ``_ensure_environment`` to return a LocalEnvironment rooted
    at a fresh subdir of ``tmp_path``. We nest under ``ws/`` so the
    conftest's autouse ``scratch/`` dir (in ``tmp_path`` root) doesn't
    leak into ``/lha/workspace/tree`` listings."""
    import asyncio

    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalEnvironment(working_dir=ws)

    async def _stub_ensure(_user_id: str) -> LocalEnvironment:
        return env

    # The fixture is sync; we still need to call initialize() on the env
    # so working_dir is set. LocalEnvironment.initialize() is trivial
    # (mkdir) so we drive it via a one-shot event loop. asyncio.run handles
    # the loop lifecycle cleanly regardless of whatever sibling tests left
    # behind in the main thread.
    asyncio.run(env.initialize())
    monkeypatch.setattr(session_start, "_ensure_environment", _stub_ensure)
    return env


def test_upload_happy_path_writes_file(env_at_tmp: LocalEnvironment) -> None:
    client = TestClient(_build_app())
    payload = b"name,age\nalice,30\n"
    resp = client.post(
        "/lha/uploads",
        files={"file": ("dataset.csv", io.BytesIO(payload), "text/csv")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "path": "/workspace/uploads/dataset.csv",
        "size": len(payload),
        "replaced": False,
    }
    assert (
        env_at_tmp.working_dir / "uploads" / "dataset.csv"
    ).read_bytes() == payload


def test_upload_creates_uploads_subdir_when_missing(
    env_at_tmp: LocalEnvironment,
) -> None:
    assert not (env_at_tmp.working_dir / "uploads").exists()
    client = TestClient(_build_app())
    resp = client.post(
        "/lha/uploads",
        files={"file": ("first.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    uploads_dir = env_at_tmp.working_dir / "uploads"
    assert uploads_dir.is_dir()
    assert (uploads_dir / "first.txt").read_bytes() == b"hi"


def test_upload_oversize_is_413(
    env_at_tmp: LocalEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the cap so the test doesn't have to allocate 50 MB.
    monkeypatch.setattr("horizon.api.uploads.MAX_UPLOAD_BYTES", 16)
    client = TestClient(_build_app())
    resp = client.post(
        "/lha/uploads",
        files={
            "file": (
                "big.bin",
                io.BytesIO(b"x" * 32),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 413
    assert not (env_at_tmp.working_dir / "uploads" / "big.bin").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "foo/bar.txt",
        "back\\slash.txt",
        ".hidden",
        ".",
        "..",
    ],
)
def test_upload_path_traversal_is_400(
    env_at_tmp: LocalEnvironment, name: str
) -> None:
    client = TestClient(_build_app())
    resp = client.post(
        "/lha/uploads",
        files={"file": (name, io.BytesIO(b"x"), "text/plain")},
    )
    assert resp.status_code == 400, (name, resp.text)


def test_upload_with_relpath_preserves_tree(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    resp = client.post(
        "/lha/uploads",
        files={
            "file": ("main.py", io.BytesIO(b"print('hi')"), "text/x-python")
        },
        data={"relpath": "myproj/src/main.py"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "/workspace/uploads/myproj/src/main.py"
    written = env_at_tmp.working_dir / "uploads" / "myproj" / "src" / "main.py"
    assert written.read_bytes() == b"print('hi')"


def test_upload_with_relpath_accepts_backslashes(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    resp = client.post(
        "/lha/uploads",
        files={"file": ("main.py", io.BytesIO(b"x"), "text/x-python")},
        data={"relpath": "myproj\\src\\main.py"},
    )
    assert resp.status_code == 200, resp.text
    assert (
        env_at_tmp.working_dir / "uploads" / "myproj" / "src" / "main.py"
    ).exists()


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.py",
        "foo/../bar.py",
        "foo/./bar.py",
        "/abs/path.py",
    ],
)
def test_upload_with_relpath_rejects_traversal(
    env_at_tmp: LocalEnvironment, bad: str
) -> None:
    client = TestClient(_build_app())
    resp = client.post(
        "/lha/uploads",
        files={"file": ("main.py", io.BytesIO(b"x"), "text/x-python")},
        data={"relpath": bad},
    )
    assert resp.status_code == 400, (bad, resp.text)


def test_upload_overwrite_reports_replaced_true(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    client.post(
        "/lha/uploads",
        files={"file": ("data.csv", io.BytesIO(b"v1"), "text/csv")},
    )
    resp = client.post(
        "/lha/uploads",
        files={"file": ("data.csv", io.BytesIO(b"v2-longer"), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["replaced"] is True
    assert (
        env_at_tmp.working_dir / "uploads" / "data.csv"
    ).read_bytes() == b"v2-longer"


def test_upload_missing_user_id_is_401(env_at_tmp: LocalEnvironment) -> None:
    # Build app WITHOUT the dependency_override so the real
    # current_user_id runs; with no IdentityMiddleware in the test app
    # and LHA_AUTH_MODE=iap, it returns 401.
    app = FastAPI()
    attach_uploads_routes(app)
    import os

    os.environ["LHA_AUTH_MODE"] = "iap"
    os.environ["LHA_IAP_AUDIENCE"] = "/projects/0/locations/x/services/y"
    try:
        client = TestClient(app)
        resp = client.post(
            "/lha/uploads",
            files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")},
        )
        assert resp.status_code == 401
    finally:
        os.environ.pop("LHA_AUTH_MODE", None)
        os.environ.pop("LHA_IAP_AUDIENCE", None)


def test_tree_happy_path_lists_files_and_dirs(
    env_at_tmp: LocalEnvironment,
) -> None:
    (env_at_tmp.working_dir / "a.txt").write_text("hello")
    (env_at_tmp.working_dir / "b.csv").write_text("a,b\n1,2\n")
    (env_at_tmp.working_dir / "sub").mkdir()

    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/tree")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "/workspace"
    assert body["truncated"] is False
    by_name = {e["name"]: e for e in body["entries"]}
    assert set(by_name) == {"a.txt", "b.csv", "sub"}
    assert by_name["a.txt"]["kind"] == "file"
    assert by_name["a.txt"]["size"] == 5
    assert by_name["sub"]["kind"] == "dir"


def test_tree_empty_workspace_returns_empty(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/tree")
    assert resp.status_code == 200
    assert resp.json() == {
        "path": "/workspace",
        "entries": [],
        "truncated": False,
    }


def test_tree_path_outside_workspace_is_400(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/tree", params={"path": "/etc"})
    assert resp.status_code == 400


def test_tree_subdir_returns_only_its_entries(
    env_at_tmp: LocalEnvironment,
) -> None:
    sub = env_at_tmp.working_dir / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("xyz")
    (env_at_tmp.working_dir / "outer.txt").write_text("not me")

    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/tree", params={"path": "/workspace/sub"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "/workspace/sub"
    assert [e["name"] for e in body["entries"]] == ["inner.txt"]


def test_tree_excludes_cache_junk(env_at_tmp: LocalEnvironment) -> None:
    (env_at_tmp.working_dir / "keep.py").write_text("x")
    (env_at_tmp.working_dir / "__pycache__").mkdir()
    (env_at_tmp.working_dir / "module.pyc").write_text("x")
    (env_at_tmp.working_dir / ".DS_Store").write_text("x")
    (env_at_tmp.working_dir / "node_modules").mkdir()
    (env_at_tmp.working_dir / ".pytest_cache").mkdir()

    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/tree")
    assert resp.status_code == 200, resp.text
    names = {e["name"] for e in resp.json()["entries"]}
    assert names == {"keep.py"}


def test_tree_truncates_at_max_entries(env_at_tmp: LocalEnvironment) -> None:
    # Create one more file than the cap to exercise the real script's
    # truncation branch (no env.execute stubbing — we want to catch
    # regressions in the inline Python too).
    for i in range(MAX_TREE_ENTRIES + 5):
        (env_at_tmp.working_dir / f"f{i:04d}.txt").write_text("x")

    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/tree")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["entries"]) == MAX_TREE_ENTRIES
    assert body["truncated"] is True


def test_download_happy_path_returns_file_bytes(
    env_at_tmp: LocalEnvironment,
) -> None:
    payload = b"name,age\nalice,30\nbob,40\n"
    (env_at_tmp.working_dir / "dataset.csv").write_bytes(payload)

    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download", params={"path": "/workspace/dataset.csv"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.content == payload
    assert resp.headers["content-type"] == "application/octet-stream"
    assert "dataset.csv" in resp.headers["content-disposition"]


def test_download_inline_text_uses_inferred_mime(
    env_at_tmp: LocalEnvironment,
) -> None:
    (env_at_tmp.working_dir / "notes.txt").write_text("hello world")

    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download",
        params={"path": "/workspace/notes.txt", "inline": "1"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    disp = resp.headers["content-disposition"]
    assert disp.startswith("inline;")
    assert "notes.txt" in disp
    # Defense-in-depth headers must always accompany inline responses.
    assert resp.headers["content-security-policy"] == "sandbox"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_download_inline_html_keeps_csp_sandbox(
    env_at_tmp: LocalEnvironment,
) -> None:
    (env_at_tmp.working_dir / "report.html").write_text(
        "<!doctype html><script>alert(1)</script>"
    )

    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download",
        params={"path": "/workspace/report.html", "inline": "1"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-security-policy"] == "sandbox"


def test_download_inline_unknown_mime_falls_back_to_attachment(
    env_at_tmp: LocalEnvironment,
) -> None:
    (env_at_tmp.working_dir / "blob.bin").write_bytes(b"\x00\x01\x02")

    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download",
        params={"path": "/workspace/blob.bin", "inline": "1"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert "content-security-policy" not in {k.lower() for k in resp.headers}


def test_download_default_remains_attachment(
    env_at_tmp: LocalEnvironment,
) -> None:
    (env_at_tmp.working_dir / "notes.txt").write_text("hello")
    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download", params={"path": "/workspace/notes.txt"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment;")


def test_download_missing_file_is_404(env_at_tmp: LocalEnvironment) -> None:
    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download", params={"path": "/workspace/missing.csv"}
    )
    assert resp.status_code == 404


def test_download_directory_returns_zip(env_at_tmp: LocalEnvironment) -> None:
    sub = env_at_tmp.working_dir / "subdir"
    sub.mkdir()
    (sub / "a.txt").write_bytes(b"hello")
    (sub / "nested").mkdir()
    (sub / "nested" / "b.txt").write_bytes(b"world")

    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download", params={"path": "/workspace/subdir"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="subdir.zip"' in resp.headers["content-disposition"]

    import zipfile

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["a.txt", "nested/b.txt"]
        assert zf.read("a.txt") == b"hello"
        assert zf.read("nested/b.txt") == b"world"


def test_download_outside_workspace_is_400(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    resp = client.get("/lha/workspace/download", params={"path": "/etc/passwd"})
    assert resp.status_code == 400


def test_download_oversize_is_413(
    env_at_tmp: LocalEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("horizon.api.uploads.MAX_UPLOAD_BYTES", 16)
    (env_at_tmp.working_dir / "big.bin").write_bytes(b"x" * 32)
    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download", params={"path": "/workspace/big.bin"}
    )
    assert resp.status_code == 413
    # Body must NOT contain the file bytes.
    assert b"x" * 32 not in resp.content


def test_download_unicode_filename_round_trips(
    env_at_tmp: LocalEnvironment,
) -> None:
    name = "résumé.csv"
    (env_at_tmp.working_dir / name).write_bytes(b"col\nval\n")
    client = TestClient(_build_app())
    resp = client.get(
        "/lha/workspace/download", params={"path": f"/workspace/{name}"}
    )
    assert resp.status_code == 200, resp.text
    disp = resp.headers["content-disposition"]
    # RFC 5987 segment present and percent-encoded UTF-8.
    assert "filename*=UTF-8''" in disp
    assert "r%C3%A9sum%C3%A9.csv" in disp


def test_download_missing_user_id_is_401(env_at_tmp: LocalEnvironment) -> None:
    app = FastAPI()
    attach_uploads_routes(app)
    import os

    os.environ["LHA_AUTH_MODE"] = "iap"
    os.environ["LHA_IAP_AUDIENCE"] = "/projects/0/locations/x/services/y"
    try:
        client = TestClient(app)
        resp = client.get(
            "/lha/workspace/download", params={"path": "/workspace/x.txt"}
        )
        assert resp.status_code == 401
    finally:
        os.environ.pop("LHA_AUTH_MODE", None)
        os.environ.pop("LHA_IAP_AUDIENCE", None)


def test_delete_happy_path_removes_file(env_at_tmp: LocalEnvironment) -> None:
    target = env_at_tmp.working_dir / "uploads" / "doomed.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"bye")

    client = TestClient(_build_app())
    resp = client.delete(
        "/lha/workspace/file", params={"path": "/workspace/uploads/doomed.txt"}
    )

    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    assert not target.exists()


def test_delete_missing_file_is_404(env_at_tmp: LocalEnvironment) -> None:
    client = TestClient(_build_app())
    resp = client.delete(
        "/lha/workspace/file", params={"path": "/workspace/uploads/nope.txt"}
    )
    assert resp.status_code == 404


def test_delete_outside_workspace_is_400(env_at_tmp: LocalEnvironment) -> None:
    client = TestClient(_build_app())
    resp = client.delete("/lha/workspace/file", params={"path": "/etc/passwd"})
    assert resp.status_code == 400


def test_delete_workspace_root_is_400(env_at_tmp: LocalEnvironment) -> None:
    client = TestClient(_build_app())
    resp = client.delete("/lha/workspace/file", params={"path": "/workspace"})
    assert resp.status_code == 400


def test_delete_directory_without_recursive_is_400(
    env_at_tmp: LocalEnvironment,
) -> None:
    (env_at_tmp.working_dir / "sub").mkdir()
    client = TestClient(_build_app())
    resp = client.delete(
        "/lha/workspace/file", params={"path": "/workspace/sub"}
    )
    assert resp.status_code == 400
    assert "recursive" in resp.json()["detail"].lower()
    assert (env_at_tmp.working_dir / "sub").is_dir()


def test_delete_directory_recursive_removes_tree(
    env_at_tmp: LocalEnvironment,
) -> None:
    sub = env_at_tmp.working_dir / "sub"
    sub.mkdir()
    (sub / "a.txt").write_bytes(b"x")
    (sub / "nested").mkdir()
    (sub / "nested" / "b.txt").write_bytes(b"y")

    client = TestClient(_build_app())
    resp = client.delete(
        "/lha/workspace/file",
        params={"path": "/workspace/sub", "recursive": "true"},
    )
    assert resp.status_code == 204, resp.text
    assert not sub.exists()


def test_delete_recursive_workspace_root_is_400(
    env_at_tmp: LocalEnvironment,
) -> None:
    client = TestClient(_build_app())
    resp = client.delete(
        "/lha/workspace/file",
        params={"path": "/workspace", "recursive": "true"},
    )
    assert resp.status_code == 400
    assert env_at_tmp.working_dir.exists()


def test_delete_missing_user_id_is_401(env_at_tmp: LocalEnvironment) -> None:
    app = FastAPI()
    attach_uploads_routes(app)
    import os

    os.environ["LHA_AUTH_MODE"] = "iap"
    os.environ["LHA_IAP_AUDIENCE"] = "/projects/0/locations/x/services/y"
    try:
        client = TestClient(app)
        resp = client.delete(
            "/lha/workspace/file", params={"path": "/workspace/uploads/x.txt"}
        )
        assert resp.status_code == 401
    finally:
        os.environ.pop("LHA_AUTH_MODE", None)
        os.environ.pop("LHA_IAP_AUDIENCE", None)


# ---------------------------------------------------------------------------
# PUT /lha/workspace/file — browser-side editing of an existing text file.
# ---------------------------------------------------------------------------


def test_write_overwrites_existing_file(env_at_tmp: LocalEnvironment) -> None:
    target = env_at_tmp.working_dir / "report.md"
    target.write_text("# old\n", encoding="utf-8")

    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/report.md"},
        json={"content": "# new\n\nedited\n"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"path": "/workspace/report.md", "size": 14}
    assert target.read_text(encoding="utf-8") == "# new\n\nedited\n"


def test_write_missing_file_is_404(env_at_tmp: LocalEnvironment) -> None:
    """Edit-only: creation stays on POST /lha/uploads so this route can't
    be used to drop new files anywhere under the workspace."""
    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/nope.md"},
        json={"content": "x"},
    )
    assert resp.status_code == 404
    assert not (env_at_tmp.working_dir / "nope.md").exists()


def test_write_directory_is_400(env_at_tmp: LocalEnvironment) -> None:
    (env_at_tmp.working_dir / "docs").mkdir()
    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/docs"},
        json={"content": "x"},
    )
    assert resp.status_code == 400


def test_write_outside_workspace_is_400(env_at_tmp: LocalEnvironment) -> None:
    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/../../etc/passwd"},
        json={"content": "pwned"},
    )
    assert resp.status_code == 400


def test_write_binary_extension_is_415(env_at_tmp: LocalEnvironment) -> None:
    target = env_at_tmp.working_dir / "logo.png"
    target.write_bytes(b"\x89PNG\r\n")

    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/logo.png"},
        json={"content": "not a png"},
    )
    assert resp.status_code == 415
    assert target.read_bytes() == b"\x89PNG\r\n"


def test_write_protected_path_is_403(
    env_at_tmp: LocalEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser route honours the same protected-paths list as the
    agent's own write tool — the UI must not be a way around it."""
    target = env_at_tmp.working_dir / "secrets.txt"
    target.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("horizon.api.uploads._is_write_denied", lambda _p: True)

    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/secrets.txt"},
        json={"content": "clobbered"},
    )
    assert resp.status_code == 403
    assert target.read_text(encoding="utf-8") == "keep"


def test_write_oversized_content_is_413(env_at_tmp: LocalEnvironment) -> None:
    from horizon.api.uploads import MAX_EDIT_BYTES

    target = env_at_tmp.working_dir / "big.md"
    target.write_text("small", encoding="utf-8")

    client = TestClient(_build_app())
    resp = client.put(
        "/lha/workspace/file",
        params={"path": "/workspace/big.md"},
        json={"content": "x" * (MAX_EDIT_BYTES + 1)},
    )
    assert resp.status_code == 413
    assert target.read_text(encoding="utf-8") == "small"
