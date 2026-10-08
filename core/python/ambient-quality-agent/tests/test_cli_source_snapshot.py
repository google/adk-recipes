# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`aqua publish-source`, and the snapshot `attach --source-root` takes.

The packing and the batching are `ambient_quality_cli.source_snapshot`; the
command line around them is faked at `_fetch_command_result` and
`_build_client`, the seams every route call goes through. The engine side is
`tests/test_source_code_upload.py`, and the two together end to end are
`tests/test_source_snapshot_contract.py`.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import source_snapshot as lib
from click.testing import CliRunner

ENGINE = "projects/123456789012/locations/us-east1/reasoningEngines/42"


def _build_revision(revision_id: str, create_time: str) -> dict[str, Any]:
    return {
        "name": f"{ENGINE}/runtimeRevisions/{revision_id}",
        "createTime": create_time,
    }


def _create_project(
    root: Path, files: dict[str, bytes], *, metadata=True
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if metadata:
        (root / lib.METADATA_FILE).write_text(
            json.dumps(
                {"remote_agent_runtime_id": ENGINE, "agent_directory": "app"}
            ),
            encoding="utf-8",
        )
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _decode(packed: lib.PackedFile) -> bytes:
    return gzip.decompress(base64.b64decode(packed.content))


# --- the revision ------------------------------------------------------------ #


def test_newest_revision_goes_by_time_not_by_id() -> None:
    revisions = [
        _build_revision("10", "2026-09-01T00:00:00Z"),
        _build_revision("3", "2026-09-03T00:00:00Z"),
        _build_revision("7", "2026-09-02T00:00:00Z"),
    ]

    assert lib.resolve_newest_revision(revisions) == "3"


def test_one_undated_revision_fails_the_whole_selection() -> None:
    with pytest.raises(lib.SnapshotError, match="createTime"):
        lib.resolve_newest_revision(
            [_build_revision("10", "2026-09-01T00:00:00Z"), {"name": "x/11"}]
        )


def test_an_engine_without_revisions_fails() -> None:
    with pytest.raises(lib.SnapshotError, match="no runtime revisions"):
        lib.resolve_newest_revision([])


class _Pages:
    """A `JsonApi` serving revision pages in order."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, Any]] = []

    def get_json(self, url: str, params=None) -> dict[str, Any]:
        self.calls.append((url, params))
        return self.pages.pop(0)

    def post_json(self, url: str, body) -> dict[str, Any]:
        raise AssertionError("revisions are only read")


def test_list_revisions_reads_every_page_of_v1beta1() -> None:
    api = _Pages(
        {
            "reasoningEngineRuntimeRevisions": [
                _build_revision("1", "2026-01-01")
            ],
            "nextPageToken": "t",
        },
        {
            "reasoningEngineRuntimeRevisions": [
                _build_revision("2", "2026-01-02")
            ]
        },
    )

    revisions = lib.list_revisions(ENGINE, api)

    assert [r["name"].rsplit("/", 1)[-1] for r in revisions] == ["1", "2"]
    assert api.calls == [
        (
            f"https://us-east1-aiplatform.googleapis.com/v1beta1/{ENGINE}"
            "/runtimeRevisions",
            None,
        ),
        (
            f"https://us-east1-aiplatform.googleapis.com/v1beta1/{ENGINE}"
            "/runtimeRevisions",
            {"pageToken": "t"},
        ),
    ]


def test_metadata_is_the_agents_own_not_aquas(tmp_path: Path) -> None:
    root = _create_project(tmp_path / "p", {})
    (root / ".aqua").mkdir()
    (root / ".aqua" / lib.METADATA_FILE).write_text(
        json.dumps({"remote_agent_runtime_id": "aqua"}), encoding="utf-8"
    )

    assert lib.load_deployment_metadata(root)["remote_agent_runtime_id"] == (
        ENGINE
    )


def test_missing_metadata_says_to_deploy_first(tmp_path: Path) -> None:
    with pytest.raises(lib.SnapshotError, match="deploy the observed agent"):
        lib.load_deployment_metadata(tmp_path)


# --- the file set ------------------------------------------------------------ #


def test_gcloudignore_wins_over_gitignore(tmp_path: Path) -> None:
    _create_project(
        tmp_path,
        {".gcloudignore": b"build/\n", ".gitignore": b"never/\n"},
        metadata=False,
    )

    lines = lib._read_ignore_lines(tmp_path)

    assert "build/" in lines
    assert "never/" not in lines


def test_include_directive_is_expanded_once(tmp_path: Path) -> None:
    _create_project(
        tmp_path,
        {
            ".gcloudignore": b"#!include:.gitignore\ndist/\n",
            ".gitignore": b"#!include:nested.txt\n*.pyc\n",
            "nested.txt": b"not-expanded/\n",
        },
        metadata=False,
    )

    lines = lib._read_ignore_lines(tmp_path)

    assert "*.pyc" in lines and "dist/" in lines
    assert "not-expanded/" not in lines


def test_packaged_files_match_the_deployed_selection(tmp_path: Path) -> None:
    pytest.importorskip("pathspec")
    _create_project(
        tmp_path,
        {
            ".gcloudignore": b"#!include:.gitignore\nbuild/\n",
            ".gitignore": b"*.secret\n",
            "app/agent.py": b"code",
            "app/config.secret": b"hidden",
            "build/artifact.bin": b"generated",
            "README.md": b"docs",
        },
        metadata=False,
    )

    paths, ignore_patterns = lib.list_packaged_files(tmp_path)

    assert paths == ["README.md", "app/agent.py"]
    assert ignore_patterns == [
        ".git",
        ".gcloudignore",
        ".gitignore",
        "*.secret",
        "build/",
    ]


# --- packing ----------------------------------------------------------------- #


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch):
    """Fixes the file set, so packing is tested apart from the ignore walk."""

    def install(paths: list[str]) -> None:
        monkeypatch.setattr(
            lib, "list_packaged_files", lambda root: (paths, [".git"])
        )

    return install


def test_an_oversized_file_is_truncated_and_flagged(
    tmp_path: Path, packaged, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lib, "PER_FILE_LIMIT", 8)
    root = _create_project(tmp_path, {"big.py": b"z" * 20, "small.py": b"ok"})
    packaged(["big.py", "small.py"])

    packed = lib.pack(root, engine=ENGINE, agent_directory="app")

    assert packed.manifest["files"] == [
        {"path": "big.py", "size": 8, "truncated": True},
        {"path": "small.py", "size": 2, "truncated": False},
    ]
    assert [_decode(f) for f in packed.files] == [b"z" * 8, b"ok"]
    assert packed.truncated_files == 1


def test_the_total_cap_stops_packing_and_counts_the_rest(
    tmp_path: Path, packaged, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lib, "TOTAL_LIMIT", 10)
    root = _create_project(
        tmp_path, {"a.py": b"123456", "b.py": b"123456", "c.py": b"1"}
    )
    packaged(["a.py", "b.py", "c.py"])

    packed = lib.pack(root, engine=ENGINE, agent_directory="")

    assert [f.path for f in packed.files] == ["a.py"]
    assert packed.manifest["omitted_files"] == 2


def test_an_unreadable_file_is_omitted_not_fatal(
    tmp_path: Path, packaged
) -> None:
    root = _create_project(tmp_path, {"a.py": b"a"})
    packaged(["gone.py", "a.py"])

    packed = lib.pack(root, engine=ENGINE, agent_directory="")

    assert [f.path for f in packed.files] == ["a.py"]
    assert packed.manifest["omitted_files"] == 1


def test_media_files_are_left_out(tmp_path: Path, packaged) -> None:
    """A root-cause analysis session reads the snapshot as text; any other
    file is sent, whatever its type."""
    root = _create_project(
        tmp_path,
        {
            "app/agent.py": b"a",
            "app/prompt.MD": b"p",
            "Dockerfile": b"FROM x",
            "app/logo.png": b"\x89PNG",
            "app/demo.mp4": b"\0",
            "app/chime.wav": b"\0",
            "app/schema.avsc": b"{}",
        },
    )
    packaged(
        [
            "Dockerfile",
            "app/agent.py",
            "app/chime.wav",
            "app/demo.mp4",
            "app/logo.png",
            "app/prompt.MD",
            "app/schema.avsc",
        ]
    )

    packed = lib.pack(root, engine=ENGINE, agent_directory="")

    assert [f.path for f in packed.files] == [
        "Dockerfile",
        "app/agent.py",
        "app/prompt.MD",
        "app/schema.avsc",
    ]
    assert packed.skipped_files == 3
    assert packed.manifest["ignore_patterns"][-len(lib.MEDIA_PATTERNS) :] == [
        *lib.MEDIA_PATTERNS
    ]
    assert packed.manifest["omitted_files"] == 0


def test_source_fits_whole_and_only_noise_is_truncated_to_fit(
    tmp_path: Path, packaged
) -> None:
    """1 MiB of source compresses below a batch; 1 MiB of noise cannot."""
    text = (b"def handler(request):\n    return respond(request)\n" * 30000)[
        : lib.PER_FILE_LIMIT
    ]
    noise = os.urandom(lib.PER_FILE_LIMIT)
    root = _create_project(
        tmp_path, {"app/code.py": text, "app/blob.txt": noise}
    )
    packaged(["app/code.py", "app/blob.txt"])

    packed = lib.pack(root, engine=ENGINE, agent_directory="")

    code, logo = packed.manifest["files"]
    assert code == {
        "path": "app/code.py",
        "size": len(text),
        "truncated": False,
    }
    assert logo["truncated"] and logo["size"] < len(noise)
    assert _decode(packed.files[1]) == noise[: logo["size"]]
    assert all(len(f.content) <= lib.BATCH_BYTES for f in packed.files)


def test_batches_stay_under_the_budget_and_the_file_count() -> None:
    files = [
        lib.PackedFile(path=f"f{i}.py", content="x" * 1000) for i in range(450)
    ]
    big = [lib.PackedFile(path="big", content="y" * (lib.BATCH_BYTES - 100))]

    batches = lib.build_batches(files + big)

    assert [len(b) for b in batches] == [200, 200, 50, 1]
    assert [f for b in batches for f in b] == files + big


def test_upload_commits_the_manifest_after_every_batch() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(route: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((route, body))
        return {"uri": "gs://b/a/1/manifest.json"} if route == "c" else {}

    packed = lib.Snapshot(
        files=[lib.PackedFile(path=f"f{i}", content="x") for i in range(201)],
        manifest={"files": [], "omitted_files": 0},
    )

    reply = lib.upload(
        post,
        agent_name="a",
        revision="1",
        snapshot=packed,
        upload_route="u",
        commit_route="c",
    )

    assert [route for route, _ in calls] == ["u", "u", "c"]
    assert calls[0][1]["agent_name"] == "a" and calls[0][1]["revision"] == "1"
    assert reply == {"uri": "gs://b/a/1/manifest.json"}


def test_a_manifest_too_large_for_one_request_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lib, "MAX_BODY_BYTES", 4096)
    sent: list[str] = []
    packed = lib.Snapshot(
        files=[lib.PackedFile(path="f", content="x")],
        manifest={
            "files": [
                {"path": f"dir/file{i}.py", "size": 1, "truncated": False}
                for i in range(100)
            ],
            "omitted_files": 0,
        },
    )

    def post(route: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append(route)
        return {}

    with pytest.raises(lib.SnapshotError, match=r"\.gcloudignore"):
        lib.upload(
            post,
            agent_name="a",
            revision="1",
            snapshot=packed,
            upload_route="u",
            commit_route="c",
        )
    assert sent == []


def test_a_refused_batch_stops_before_the_manifest() -> None:
    calls: list[str] = []

    def post(route: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append(route)
        return {"error": "no source bucket"}

    packed = lib.Snapshot(
        files=[lib.PackedFile(path="f", content="x")],
        manifest={"files": [], "omitted_files": 0},
    )

    with pytest.raises(lib.SnapshotError, match="no source bucket"):
        lib.upload(
            post,
            agent_name="a",
            revision="1",
            snapshot=packed,
            upload_route="u",
            commit_route="c",
        )
    assert calls == ["u"]


# --- which agent ------------------------------------------------------------- #


def test_the_attachment_recording_the_engine_wins() -> None:
    listing = {
        "agents": [
            {"agent_name": "watched", "watched": True},
            {"agent_name": "other", "observed_engine_id": "42"},
        ],
        "environment_agent": None,
    }

    assert lib.resolve_agent_for_engine(listing, ENGINE) == "other"


def test_without_a_matching_attachment_the_watched_agent() -> None:
    """One attached without an engine, which may well be this one."""
    listing = {
        "agents": [{"agent_name": "watched", "watched": True}],
        "environment_agent": None,
    }

    assert lib.resolve_agent_for_engine(listing, ENGINE) == "watched"


def test_a_watched_agent_recording_another_engine_is_not_chosen() -> None:
    """Its root-cause analysis would cite this engine's code as its own."""
    listing = {
        "agents": [
            {
                "agent_name": "watched",
                "watched": True,
                "observed_engine_id": "7",
            }
        ],
        "environment_agent": None,
    }

    assert lib.resolve_agent_for_engine(listing, ENGINE) == ""


def test_with_nothing_attached_the_agent_the_environment_names() -> None:
    assert (
        lib.resolve_agent_for_engine(
            {"agents": [], "environment_agent": "env_agent"}, ENGINE
        )
        == "env_agent"
    )


# --- the command ------------------------------------------------------------- #


class _FakeAqua:
    """AQuA's routes, as `_fetch_command_result` and a client reach them."""

    def __init__(self, *, published: str = "") -> None:
        self.published = published
        self.calls: list[tuple[str, Any]] = []
        self.listing = {
            "agents": [
                {"agent_name": "travel_agent", "observed_engine_id": "42"}
            ],
            "environment_agent": None,
        }

    def fetch(self, *, url, resource, path, payload=None) -> Any:
        self.calls.append((path, payload))
        if path == "agents/list":
            return self.listing
        if path == "source":
            if self.published:
                return {
                    "available": True,
                    "revision": self.published,
                    "uri": "gs://b/manifest.json",
                }
            return {"available": False, "reason": "none yet"}
        return self.answer(path, payload)

    def answer(self, path: str, payload: Any) -> dict[str, Any]:
        if path == "source/upload":
            return {"written": len(payload["files"])}
        if path == "source/commit":
            return {
                "agent_name": payload["agent_name"],
                "revision": payload["revision"],
                "uri": "gs://b/travel_agent/9/manifest.json",
            }
        if path == "agents/attach":
            return {
                "agent_name": "travel_agent",
                "watched": True,
                "attached": True,
            }
        raise AssertionError(f"unexpected route {path}")

    def routes(self) -> list[str]:
        return [path for path, _ in self.calls]


class _FakeClient:
    def __init__(self, aqua: _FakeAqua) -> None:
        self._aqua = aqua

    async def post_command(self, path: str, payload=None) -> dict[str, Any]:
        self._aqua.calls.append((path, payload))
        return self._aqua.answer(path, payload)


@pytest.fixture
def aqua(monkeypatch: pytest.MonkeyPatch) -> _FakeAqua:
    fake = _FakeAqua()
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", fake.fetch)
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda url, resource: _FakeClient(fake)
    )
    monkeypatch.setattr(aqua_cli, "get_adc_token", lambda: "token")
    monkeypatch.setattr(
        lib,
        "list_revisions",
        lambda engine, api: [
            _build_revision("9", "2026-09-02T00:00:00Z"),
            _build_revision("8", "2026-09-01T00:00:00Z"),
        ],
    )
    return fake


def _publish(root: Path, *args: str):
    return CliRunner().invoke(
        aqua_cli.cli,
        [
            "publish-source",
            *args,
            "--source-root",
            str(root),
            "--resource",
            "r",
        ],
    )


def test_publish_source_uploads_the_newest_revision_for_the_engines_agent(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    pytest.importorskip("pathspec")
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x = 1\n"})

    result = _publish(root)

    assert result.exit_code == 0, result.output
    assert aqua.routes() == [
        "agents/list",
        "source",
        "source/upload",
        "source/commit",
    ]
    assert aqua.calls[1][1] == {"agent_name": "travel_agent", "revision": "9"}
    upload = aqua.calls[2][1]
    assert upload["agent_name"] == "travel_agent" and upload["revision"] == "9"
    assert {f["path"] for f in upload["files"]} == {
        "app/agent.py",
        lib.METADATA_FILE,
    }
    manifest = aqua.calls[3][1]["manifest"]
    assert manifest["engine"] == ENGINE
    assert manifest["agent_directory"] == "app"
    assert json.loads(result.stdout)["published"] is True


def test_an_already_published_revision_is_not_uploaded_again(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    aqua.published = "9"
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x"})

    result = _publish(root, "travel_agent")

    assert result.exit_code == 0, result.output
    assert aqua.routes() == ["source"]
    assert json.loads(result.stdout)["published"] is False


def test_force_uploads_a_published_revision_again(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    pytest.importorskip("pathspec")
    aqua.published = "3"
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x"})

    result = _publish(root, "travel_agent", "--force", "--revision", "3")

    assert result.exit_code == 0, result.output
    assert aqua.routes() == ["source/upload", "source/commit"]
    assert aqua.calls[0][1]["revision"] == "3"


def test_another_revision_being_published_does_not_skip_this_one(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    """An engine that ignores `revision` on the route reports its newest."""
    pytest.importorskip("pathspec")
    aqua.published = "8"
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x"})

    result = _publish(root, "travel_agent")

    assert result.exit_code == 0, result.output
    assert "source/commit" in aqua.routes()


def test_a_project_deployed_to_another_engine_is_refused(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x"})

    result = _publish(
        root,
        "--observed-agent-resource",
        "projects/p/locations/us-east1/reasoningEngines/7",
    )

    assert result.exit_code == 1
    assert (
        "not projects/p/locations/us-east1/reasoningEngines/7" in result.output
    )
    assert aqua.routes() == []


def test_a_dry_run_uploads_nothing(aqua: _FakeAqua, tmp_path: Path) -> None:
    pytest.importorskip("pathspec")
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x"})

    result = _publish(root, "travel_agent", "--dry-run")

    assert result.exit_code == 0, result.output
    assert aqua.routes() == []
    assert json.loads(result.stdout)["file_count"] == 2


def test_an_undeployed_project_fails_with_the_reason(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    root = _create_project(tmp_path / "p", {"a.py": b"x"}, metadata=False)

    result = _publish(root)

    assert result.exit_code == 1
    assert "deploy the observed agent first" in result.output
    assert aqua.routes() == []


# --- attach takes the first snapshot ---------------------------------------- #


def _attach(root: Path, *args: str):
    return CliRunner().invoke(
        aqua_cli.cli,
        [
            "attach",
            "travel_agent",
            "--no-discover",
            "--yes",
            *args,
            "--source-root",
            str(root),
            "--resource",
            "r",
        ],
    )


def test_attach_publishes_the_source_once_attached(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    pytest.importorskip("pathspec")
    root = _create_project(tmp_path / "p", {"app/agent.py": b"x"})

    result = _attach(root)

    assert result.exit_code == 0, result.output
    routes = aqua.routes()
    assert routes.index("source/commit") > max(
        i for i, r in enumerate(routes) if r == "agents/attach"
    )
    assert aqua.calls[routes.index("source")][1] == {
        "agent_name": "travel_agent",
        "revision": "9",
    }


def test_a_failed_snapshot_does_not_fail_the_attach(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    root = _create_project(tmp_path / "p", {"a.py": b"x"}, metadata=False)

    result = _attach(root)

    assert result.exit_code == 0, result.output
    assert "No source snapshot was published" in result.output
    assert "agents-cli aqua publish-source" in result.output


def test_a_dry_run_attach_publishes_nothing(
    aqua: _FakeAqua, tmp_path: Path
) -> None:
    root = _create_project(tmp_path / "p", {"a.py": b"x"})

    result = _attach(root, "--dry-run")

    assert result.exit_code == 0, result.output
    assert not {"source", "source/upload"} & set(aqua.routes())
