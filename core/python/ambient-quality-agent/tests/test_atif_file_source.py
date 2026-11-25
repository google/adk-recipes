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

"""Tests for `AtifFileSource` over a fake GCS client and a temporary directory.

Most tests run on both backends through the `tree` fixture and expect the same
answer, since the two share every rule and differ only in file access.
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import logging
import os
import shutil
import sys
import threading
import tracemalloc
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

import pytest
from ambient_quality_agent.tools.ingestion import (
    atif_files,
    atif_references,
    trajectory_source,
)
from ambient_quality_agent.tools.ingestion.atif_files import (
    ObjectReadError,
    create_atif_files,
)
from ambient_quality_agent.tools.ingestion.atif_references import AtifLimits
from ambient_quality_agent.tools.ingestion.atif_source import (
    AtifFileSource,
    build_glob_pattern,
    is_root_trajectory_key,
)
from ambient_quality_agent.tools.ingestion.models import SamplingRecord
from ambient_quality_agent.tools.ingestion.trajectory_source import (
    MAX_ATTACHMENT_BYTES,
    AttachmentUnreadableError,
    ExternalAttachment,
    FailedDocument,
    InlineAttachment,
    LoadedDocument,
    SampledKey,
    TrajectorySource,
    WindowCount,
    check_data_url,
    decode_inline_attachment,
)
from aqua_atif import (
    DocumentRejectedError,
    IngestStatus,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    annotate_document,
    validate_document,
)
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions

from .conftest import FakeGcsClient

BUCKET = "runs-bucket"
PREFIX = "agent-a/runs"
T0 = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
START = dt.datetime(2026, 10, 14, tzinfo=dt.UTC)
END = dt.datetime(2026, 10, 16, tzinfo=dt.UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 4


# --------------------------------------------------------------------------- #
# Fakes and fixtures                                                           #
# --------------------------------------------------------------------------- #


class Tree:
    """The files under the attached prefix, on one backend."""

    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        self.client = FakeGcsClient()
        self.root = tmp_path / "runs"
        self.root.mkdir()
        self.uri = (
            f"gs://{BUCKET}/{PREFIX}" if kind == "gcs" else str(self.root)
        )

    def write(
        self, key: str, data: bytes | dict[str, Any], updated: dt.datetime = T0
    ) -> None:
        content = json.dumps(data).encode() if isinstance(data, dict) else data
        if self.kind == "gcs":
            name = (BUCKET, f"{PREFIX}/{key}")
            self.client.contents[name] = content
            self.client.objects.add(name)
            self.client.updated[name] = updated
            return
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (updated.timestamp(), updated.timestamp()))

    def build_source(self, **kwargs: Any) -> AtifFileSource:
        return AtifFileSource(
            self.uri, client_factory=lambda: self.client, **kwargs
        )

    def build_uri(self, key: str) -> str:
        if self.kind == "gcs":
            return f"gs://{BUCKET}/{PREFIX}/{key}"
        return str(self.root / key)

    def load(self, key: str, **kwargs: Any) -> LoadedDocument:
        """Loads a root that must load."""
        (loaded,) = self.build_source(**kwargs).load_trajectories([key])
        assert isinstance(loaded, LoadedDocument)
        return loaded

    def load_failure(self, key: str, **kwargs: Any) -> FailedDocument:
        """Loads a root that must fail to load."""
        (failed,) = self.build_source(**kwargs).load_trajectories([key])
        assert isinstance(failed, FailedDocument)
        return failed


@pytest.fixture(params=["gcs", "local"])
def tree(request: pytest.FixtureRequest, tmp_path: Path) -> Tree:
    return Tree(request.param, tmp_path)


@pytest.fixture
def local(tmp_path: Path) -> Tree:
    return Tree("local", tmp_path)


@pytest.fixture
def gcs(tmp_path: Path) -> Tree:
    return Tree("gcs", tmp_path)


def build_document(
    steps: list[dict[str, Any]] | None = None, **fields: Any
) -> dict[str, Any]:
    return {
        "schema_version": "ATIF-v1.8",
        "session_id": "s-1",
        "agent": {"name": "checkout-fixer", "version": "2.3"},
        "steps": steps or [{"step_id": 1, "source": "user", "message": "Hi"}],
        **fields,
    }


def build_image_step(*paths: str, step_id: int = 1) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "source": "user",
        "message": [
            {"type": "text", "text": "Fix the checkout page."},
            *(
                {
                    "type": "image",
                    "source": {"media_type": "image/png", "path": path},
                }
                for path in paths
            ),
        ],
    }


def build_delegating_step(
    *refs: dict[str, Any], step_id: int = 2
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": "",
        "tool_calls": [
            {"tool_call_id": "c1", "function_name": "delegate", "arguments": {}}
        ],
        "observation": {
            "results": [
                {
                    "source_call_id": "c1",
                    "content": "done",
                    "subagent_trajectory_ref": list(refs),
                }
            ]
        },
    }


def build_parent(*refs: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return build_document(
        [
            {"step_id": 1, "source": "user", "message": "Go"},
            build_delegating_step(*refs),
        ],
        **fields,
    )


def ingest(loaded: LoadedDocument) -> tuple[dict[str, Any], IngestStatus]:
    """Runs validation and annotation on a loaded document."""
    document = loaded.document
    validated = validate_document(document)
    status = annotate_document(
        document,
        markers=(*loaded.markers, *validated.markers),
    )
    return document, status


def read_file_key(loaded: LoadedDocument, part_path: tuple[Any, ...]) -> str:
    """Reads the key of the file that a part's `source.path` resolved to."""
    location = loaded.locations[part_path]
    assert isinstance(location, ExternalAttachment)
    return location.key


STEP_0 = ("steps", 0)
REF = ("steps", 1, "observation", "results", 0, "subagent_trajectory_ref", 0)


def build_part_marker(
    reason: Reason, detail: str | None = None, index: int = 1
):
    return PendingMissingMarker(
        STEP_0, MissingMarker(f"message[{index}]", reason, detail)
    )


def build_ref_marker(
    reason: Reason, detail: str | None = None, ref: tuple[Any, ...] = REF
) -> PendingMissingMarker:
    return PendingMissingMarker(
        ref, MissingMarker("trajectory", reason, detail)
    )


# --------------------------------------------------------------------------- #
# How a sweep drives a source                                                  #
# --------------------------------------------------------------------------- #


def test_a_sweep_counts_samples_loads_and_copies(tree: Tree):
    """The calls of one sweep, in the order ingestion makes them.

    Ingestion counts the window, samples keys, loads the sampled documents
    one at a time, and passes what the source returned beside each document
    to `aqua_atif` and to the copy step.
    """
    data_url = "data:image/gif;base64," + base64.b64encode(GIF).decode()
    tree.write("images/shot.png", PNG)
    tree.write("subagents/qa.json", build_document())
    tree.write(
        "run.atif.json",
        build_document(
            [
                build_image_step(
                    "images/shot.png", "images/gone.png", data_url
                ),
                build_delegating_step({"trajectory_path": "subagents/qa.json"}),
            ]
        ),
    )
    tree.write("plain.atif.json", build_document())
    tree.write("broken.atif.json", b"not json")
    tree.write(
        "old.atif.json", build_document(), updated=START - dt.timedelta(days=1)
    )
    source: TrajectorySource = tree.build_source()

    # 1. Count the window: the three roots, not the old root, the image or
    #    the subagent file. A sweep stops here when the count is zero.
    assert source.count_trajectories(START, END) == WindowCount(count=3)

    # 2. Sample at most `limit` keys of the same window.
    sampled = source.list_trajectory_keys(START, END, limit=10)
    assert sampled == [
        SampledKey(
            "broken.atif.json", SamplingRecord(inclusion_probability=1.0)
        ),
        SampledKey(
            "plain.atif.json", SamplingRecord(inclusion_probability=1.0)
        ),
        SampledKey("run.atif.json", SamplingRecord(inclusion_probability=1.0)),
    ]

    # 3. Load the sample. A file is read when the loop asks for the next
    #    document, and nothing raises for one document: a failed load says
    #    why and holds no document.
    outcomes: dict[str, str] = {}
    copied: list[bytes] = []
    for loaded in source.load_trajectories([item.key for item in sampled]):
        if isinstance(loaded, FailedDocument):
            outcomes[loaded.key] = loaded.reason
            continue

        # 4. Validate the document, embedded subagents included.
        validated = validate_document(loaded.document)

        # 5. Copy each attachment that resolved to bytes: a file through the
        #    source, a `data:` URL without it.
        for location in loaded.locations.values():
            if isinstance(location, ExternalAttachment):
                copied.append(source.read_attachment(location))
            elif isinstance(location, InlineAttachment):
                copied.append(
                    decode_inline_attachment(location, MAX_ATTACHMENT_BYTES)
                )

        # 6. Annotate with the markers the source returned beside the document.
        outcomes[loaded.key] = annotate_document(
            loaded.document,
            markers=[*loaded.markers, *validated.markers],
        )

    assert outcomes == {
        "broken.atif.json": "invalid JSON",
        "plain.atif.json": IngestStatus.INGESTED,
        # `images/gone.png` could not be read.
        "run.atif.json": IngestStatus.PARTIAL,
    }
    assert copied == [PNG, GIF]


# --------------------------------------------------------------------------- #
# Finding documents                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("key", "is_root"),
    [
        ("t-001.atif.json", True),
        ("trajectory.json", False),
        ("trial-1/agent/trajectory.json", False),
        ("trajectories/t-001.atif.json", True),
        ("run-1/trajectories/t-001.atif.json", False),
        ("subagents/t-001-qa.json", False),
        ("other/t-001.atif.json", False),
        ("images/t-001-shot.png", False),
        (".atif.json", False),
    ],
)
def test_the_root_rule_matches_supported_layouts(key: str, is_root: bool):
    assert is_root_trajectory_key(key) is is_root


def test_the_window_and_the_root_rule_select_the_roots(tree: Tree):
    tree.write("t-001.atif.json", build_document())
    tree.write("subagents/t-001-qa.atif.json", build_document())
    tree.write("t-001.json", build_document())
    tree.write("t-002.atif.json", build_document(), START)
    tree.write("trajectories/t-003.atif.json", build_document(), END)
    tree.write(
        "old.atif.json",
        build_document(),
        START - dt.timedelta(seconds=1),
    )
    tree.write("images/t-001-shot.png", PNG)
    source = tree.build_source()

    assert source.count_trajectories(START, END) == WindowCount(count=3)
    assert [
        item.key for item in source.list_trajectory_keys(START, END, 10)
    ] == [
        "t-001.atif.json",
        "t-002.atif.json",
        "trajectories/t-003.atif.json",
    ]


def test_atif_glob_finds_the_roots_of_harbor_trials(tree: Tree):
    tree.write("trial-1/agent/trajectory.json", build_document())
    tree.write("trial-1/user-agent/trajectory.json", build_document())
    tree.write("trial-1/agent/trajectory.cont-1.json", build_document())
    source = tree.build_source(atif_glob="**/agent/trajectory.json")

    assert [
        item.key for item in source.list_trajectory_keys(START, END, 10)
    ] == ["trial-1/agent/trajectory.json"]


def test_a_naive_window_is_read_as_utc(tree: Tree):
    tree.write("t.atif.json", build_document())
    source = tree.build_source()

    naive = (START.replace(tzinfo=None), END.replace(tzinfo=None))
    assert source.count_trajectories(*naive).count == 1


def test_atif_glob_replaces_the_root_rule(tree: Tree):
    tree.write("run-1/trajectory.atif.json", build_document())
    tree.write("run-1/sub.atif.json", build_document())
    tree.write("trial-1/agent/trajectory.json", build_document())
    source = tree.build_source(atif_glob="**/trajectory.atif.json")

    assert [
        item.key for item in source.list_trajectory_keys(START, END, 10)
    ] == ["run-1/trajectory.atif.json"]


@pytest.mark.parametrize(
    ("pattern", "key", "matches"),
    [
        ("*.json", "a.json", True),
        ("*.json", "x/a.json", False),
        ("**/*.json", "a.json", True),
        ("**/*.json", "x/y/a.json", True),
        ("runs/**", "runs/x/a.json", True),
        ("run-?/t.json", "run-1/t.json", True),
        ("run-[!2]/t.json", "run-2/t.json", False),
        ("run-[12]/t.json", "run-2/t.json", True),
        ("a+b.json", "a+b.json", True),
        ("/flat/*.json/", "flat/a.json", True),
    ],
)
def test_glob_patterns_match_whole_keys(pattern: str, key: str, matches: bool):
    assert bool(build_glob_pattern(pattern).fullmatch(key)) is matches


def test_the_sample_is_a_sorted_random_subset(tree: Tree):
    keys = [f"t-{index:02}.atif.json" for index in range(20)]
    for key in keys:
        tree.write(key, build_document())

    first = tree.build_source(seed=1).list_trajectory_keys(START, END, 5)
    second = tree.build_source(seed=2).list_trajectory_keys(START, END, 5)
    first_keys = [item.key for item in first]
    second_keys = [item.key for item in second]

    assert len(first) == 5
    assert first_keys == sorted(first_keys)
    assert set(first_keys) <= set(keys)
    assert first_keys != second_keys
    assert [item.sampling for item in first] == [
        SamplingRecord(inclusion_probability=0.25)
    ] * 5
    seeded_a = tree.build_source().list_trajectory_keys(
        START, END, 5, sampling_seed="run-seed"
    )
    seeded_b = tree.build_source().list_trajectory_keys(
        START, END, 5, sampling_seed="run-seed"
    )
    assert seeded_a == seeded_b
    assert tree.build_source().list_trajectory_keys(START, END, 0) == []


def test_a_gcs_listing_stays_inside_the_prefix(gcs: Tree):
    gcs.client.contents[(BUCKET, f"{PREFIX}2/t.atif.json")] = b"{}"
    gcs.client.updated[(BUCKET, f"{PREFIX}2/t.atif.json")] = T0
    gcs.client.contents[(BUCKET, f"{PREFIX}/folder/")] = b""
    gcs.client.updated[(BUCKET, f"{PREFIX}/folder/")] = T0
    gcs.write("t.atif.json", build_document())

    sampled = gcs.build_source().list_trajectory_keys(START, END, 10)

    assert [item.key for item in sampled] == ["t.atif.json"]


def test_a_local_listing_skips_links_that_leave_the_directory(
    local: Tree, tmp_path: Path
):
    outside = tmp_path / "outside.atif.json"
    outside.write_text(json.dumps(build_document()))
    (local.root / "link.atif.json").symlink_to(outside)
    local.write("t.atif.json", build_document())

    assert local.build_source().count_trajectories(START, END).count == 1


# --------------------------------------------------------------------------- #
# Loading a root                                                               #
# --------------------------------------------------------------------------- #


def test_a_loaded_root_carries_document_and_source_metadata(tree: Tree):
    raw = b'{"schema_version": "ATIF-v1.8", "session_id": "s",\n "agent": {"name": "a", "version": "1"}, "steps": [{"step_id": 1e0, "source": "user", "message": "x"}]}'
    tree.write("trial-1/agent/trajectory.json", raw)

    loaded = tree.load("trial-1/agent/trajectory.json")

    assert loaded.document["session_id"] == "s"
    assert loaded.source_uri == tree.build_uri("trial-1/agent/trajectory.json")
    assert loaded.source_updated_at == T0
    assert loaded.key == "trial-1/agent/trajectory.json"


def test_gcs_objects_are_read_without_decompression(gcs: Tree):
    gcs.write("t.atif.json", build_document())

    gcs.load("t.atif.json")

    assert [kwargs["raw"] for _, kwargs in gcs.client.download_requests] == [
        True
    ]


def test_a_root_over_the_cap_is_refused_from_its_metadata(tree: Tree):
    document = build_document()
    tree.write("t.atif.json", document)
    size = len(json.dumps(document))

    failed = tree.load_failure(
        "t.atif.json", limits=AtifLimits(document_bytes=size - 1)
    )

    assert failed.reason == "too_large"
    assert failed.source_uri == tree.build_uri("t.atif.json")
    assert tree.client.download_requests == []


def test_the_document_cap_holds_when_the_metadata_understates(gcs: Tree):
    gcs.write("t.atif.json", build_document())
    gcs.client.listed_sizes[(BUCKET, f"{PREFIX}/t.atif.json")] = 10

    failed = gcs.load_failure(
        "t.atif.json", limits=AtifLimits(document_bytes=20)
    )

    assert failed.reason == "too_large"
    assert gcs.client.download_requests[0][1]["end"] == 20


def test_a_compressed_object_is_read_as_stored(gcs: Tree):
    gcs.write("t.atif.json", b"\x1f\x8b\x08\x00 gzip bytes")

    assert gcs.load_failure("t.atif.json").reason == "invalid JSON"


@pytest.mark.parametrize(
    "raw",
    [b"not json", b'{"a": NaN}', b"[" * 257 + b"]" * 257],
    ids=["text", "nan", "too-deep"],
)
def test_invalid_json_is_rejected(tree: Tree, raw: bytes):
    tree.write("t.atif.json", raw)

    failed = tree.load_failure("t.atif.json")

    assert failed.reason == "invalid JSON"
    assert failed.source_updated_at == T0


@pytest.mark.parametrize("raw", [b"[]", b"null", b'"text"', b"1"])
def test_json_that_is_not_an_object_is_rejected(tree: Tree, raw: bytes):
    tree.write("t.atif.json", raw)

    failed = tree.load_failure("t.atif.json")

    assert failed.reason == "not an ATIF document"


def test_a_missing_root_is_a_read_error(tree: Tree):
    assert tree.load_failure("gone.atif.json").reason == "read_error 404"


def test_a_root_linked_outside_the_directory_is_refused(
    local: Tree, tmp_path: Path
):
    outside = tmp_path / "outside.atif.json"
    outside.write_text(json.dumps(build_document()))
    (local.root / "link.atif.json").symlink_to(outside)

    assert local.load_failure("link.atif.json").reason == "path_traversal"


def test_a_denied_gcs_root_is_a_read_error(gcs: Tree):
    gcs.write("t.atif.json", build_document())
    gcs.client.errors[(BUCKET, f"{PREFIX}/t.atif.json")] = (
        api_exceptions.Forbidden("denied")
    )

    assert gcs.load_failure("t.atif.json").reason == "read_error 403"


def test_lone_surrogates_come_back_as_markers_not_in_the_document(tree: Tree):
    tree.write(
        "t.atif.json",
        b'{"schema_version": "ATIF-v1.8", "session_id": "s", "agent": {"name": "a", "version": "1"}, "steps": [{"step_id": 1, "source": "user", "message": "x\\ud800"}]}',
    )

    loaded = tree.load("t.atif.json")

    assert loaded.document["steps"][0]["message"] == "x\ufffd"
    assert loaded.markers == (
        PendingMissingMarker(
            STEP_0,
            MissingMarker("message", Reason.INVALID_CONTENT, "lone_surrogate"),
        ),
    )
    assert "extra" not in loaded.document["steps"][0]


# --------------------------------------------------------------------------- #
# Path forms                                                                   #
# --------------------------------------------------------------------------- #


def test_a_relative_path_resolves_from_the_documents_directory(tree: Tree):
    tree.write(
        "trial-1/agent/trajectory.json",
        build_document([build_image_step("images/shot.png")]),
    )
    tree.write("trial-1/agent/images/shot.png", PNG)

    loaded = tree.load("trial-1/agent/trajectory.json")

    assert loaded.markers == ()
    assert loaded.locations == {
        (*STEP_0, "message", 1): ExternalAttachment(
            uri=tree.build_uri("trial-1/agent/images/shot.png"),
            key="trial-1/agent/images/shot.png",
            size=len(PNG),
        )
    }
    assert (
        loaded.document["steps"][0]["message"][1]["source"]["path"]
        == "images/shot.png"
    )


@pytest.mark.parametrize(
    ("path", "reason", "detail"),
    [
        ("images/missing.png", Reason.READ_ERROR, "404"),
        ("../../outside.png", Reason.PATH_TRAVERSAL, None),
        ("s3://bucket/x.png", Reason.OUT_OF_SCOPE, None),
        ("file:///etc/passwd", Reason.OUT_OF_SCOPE, None),
        ("gs://other-bucket/x.png", Reason.PATH_TRAVERSAL, None),
        (f"gs://{BUCKET}/{PREFIX}2/x.png", Reason.PATH_TRAVERSAL, None),
        (f"gs://{BUCKET}/{PREFIX}/../x.png", Reason.PATH_TRAVERSAL, None),
        ("/etc/passwd", Reason.PATH_TRAVERSAL, None),
        ("", Reason.INVALID_CONTENT, "empty_path"),
        (
            "data:image/png;base64,not base64!",
            Reason.INVALID_CONTENT,
            "data_url",
        ),
        ("data:image/png", Reason.INVALID_CONTENT, "data_url"),
    ],
)
def test_an_unread_part_gets_a_marker(
    tree: Tree, path: str, reason: Reason, detail: str | None
):
    tree.write("t.atif.json", build_document([build_image_step(path)]))

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (build_part_marker(reason, detail),)
    assert (*STEP_0, "message", 1) not in loaded.locations


def test_a_link_is_kept_and_never_fetched(tree: Tree):
    tree.write(
        "t.atif.json",
        build_document([build_image_step("https://host/shot.png")]),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    assert (*STEP_0, "message", 1) not in loaded.locations
    assert (
        loaded.document["steps"][0]["message"][1]["source"]["path"]
        == "https://host/shot.png"
    )


def test_a_data_url_that_decodes_is_measured(tree: Tree):
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    tree.write("t.atif.json", build_document([build_image_step(url)]))

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    assert loaded.locations[(*STEP_0, "message", 1)] == InlineAttachment(
        uri=url, size=len(PNG)
    )


def test_a_data_url_is_capped_after_decoding():
    url = "data:image/png;base64," + base64.b64encode(b"x" * 10).decode()

    assert check_data_url(url, 10) == InlineAttachment(uri=url, size=10)
    with pytest.raises(AttachmentUnreadableError) as over:
        check_data_url(url, 9)
    assert (over.value.reason, over.value.detail) == (Reason.TOO_LARGE, "10")
    plain = "data:text/plain,a%20b"
    assert check_data_url(plain, 3) == InlineAttachment(uri=plain, size=3)
    huge = "data:image/png;base64," + "A" * 400
    with pytest.raises(AttachmentUnreadableError) as huge_err:
        check_data_url(huge, 100)
    assert (huge_err.value.reason, huge_err.value.detail) == (
        Reason.TOO_LARGE,
        "300",
    )


def test_a_gcs_url_under_the_prefix_is_read(gcs: Tree):
    url = f"gs://{BUCKET}/{PREFIX}/images/./shot.png"
    gcs.write("t.atif.json", build_document([build_image_step(url)]))
    gcs.write("images/shot.png", PNG)

    loaded = gcs.load("t.atif.json")

    assert read_file_key(loaded, (*STEP_0, "message", 1)) == "images/shot.png"


def test_a_gcs_url_is_outside_a_local_directory(local: Tree):
    local.write("images/shot.png", PNG)
    local.write(
        "t.atif.json",
        build_document([build_image_step(f"gs://{BUCKET}/images/shot.png")]),
    )

    loaded = local.load("t.atif.json")

    assert loaded.markers == (build_part_marker(Reason.PATH_TRAVERSAL),)


def test_an_absolute_path_is_never_read(tree: Tree):
    """Even one that names a file under a local directory."""
    tree.write("images/shot.png", PNG)
    inside = str(tree.root / "images/shot.png") if tree.kind == "local" else ""
    tree.write(
        "t.atif.json",
        build_document(
            [build_image_step(inside or "/images/shot.png")],
        ),
    )
    tree.write(
        "u.atif.json", build_parent({"trajectory_path": "/subagents/qa.json"})
    )

    assert tree.load("t.atif.json").markers == (
        build_part_marker(Reason.PATH_TRAVERSAL),
    )
    assert tree.load("u.atif.json").markers == (
        build_ref_marker(Reason.PATH_TRAVERSAL),
    )


def test_a_symbolic_link_is_resolved_before_the_prefix_check(
    local: Tree, tmp_path: Path
):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    (local.root / "images").mkdir()
    (local.root / "images" / "leak.png").symlink_to(secret)
    (local.root / "images" / "real.png").write_bytes(PNG)
    (local.root / "images" / "alias.png").symlink_to("real.png")
    local.write(
        "t.atif.json",
        build_document(
            [build_image_step("images/leak.png", "images/alias.png")]
        ),
    )

    loaded = local.load("t.atif.json")

    assert loaded.markers == (build_part_marker(Reason.PATH_TRAVERSAL),)
    assert read_file_key(loaded, (*STEP_0, "message", 2)) == "images/real.png"


def test_a_linked_directory_cannot_reach_outside(local: Tree, tmp_path: Path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.png").write_bytes(PNG)
    (local.root / "images").symlink_to(tmp_path / "elsewhere")
    local.write(
        "t.atif.json", build_document([build_image_step("images/x.png")])
    )

    loaded = local.load("t.atif.json")

    assert loaded.markers == (build_part_marker(Reason.PATH_TRAVERSAL),)


def test_a_denied_attachment_is_a_read_error(gcs: Tree):
    gcs.write("t.atif.json", build_document([build_image_step("shot.png")]))
    gcs.write("shot.png", PNG)
    gcs.client.errors[(BUCKET, f"{PREFIX}/shot.png")] = (
        api_exceptions.Forbidden("denied")
    )

    loaded = gcs.load("t.atif.json")

    assert loaded.markers == (build_part_marker(Reason.READ_ERROR, "403"),)


def test_a_result_part_is_marked_on_the_result(tree: Tree):
    step = build_delegating_step()
    result = step["observation"]["results"][0]
    del result["subagent_trajectory_ref"]
    result["content"] = [
        {
            "type": "image",
            "source": {"media_type": "image/png", "path": "x.png"},
        }
    ]
    tree.write("t.atif.json", build_document([step]))

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (
        PendingMissingMarker(
            ("steps", 0, "observation", "results", 0),
            MissingMarker("content[0]", Reason.READ_ERROR, "404"),
        ),
    )


def test_ingestion_makes_an_unread_part_partial(tree: Tree):
    tree.write("t.atif.json", build_document([build_image_step("gone.png")]))

    document, status = ingest(tree.load("t.atif.json"))

    assert status == IngestStatus.PARTIAL
    assert document["steps"][0]["extra"]["aqua"]["missing"] == [
        {"path": "message[1]", "reason": "read_error", "detail": "404"}
    ]


# --------------------------------------------------------------------------- #
# Limits                                                                       #
# --------------------------------------------------------------------------- #


def test_an_attachment_over_the_cap_is_too_large(tree: Tree):
    tree.write("big.png", PNG)
    tree.write("t.atif.json", build_document([build_image_step("big.png")]))

    loaded = tree.load("t.atif.json", limits=AtifLimits(attachment_bytes=4))

    assert loaded.markers == (
        build_part_marker(Reason.TOO_LARGE, str(len(PNG))),
    )
    assert not any(
        name.endswith("big.png") for name, _ in tree.client.download_requests
    )


def test_references_past_the_cap_are_too_large(tree: Tree):
    for name in ("a.png", "b.png", "c.png"):
        tree.write(name, PNG)
    tree.write(
        "t.atif.json",
        build_document(
            [
                build_image_step(
                    "a.png", "b.png", "a.png", "https://h/x", "c.png"
                )
            ]
        ),
    )

    loaded = tree.load("t.atif.json", limits=AtifLimits(references=2))

    assert loaded.markers == (
        build_part_marker(Reason.TOO_LARGE, "2", index=5),
    )


def test_the_root_and_its_subagent_files_share_the_document_cap(tree: Tree):
    sub = build_document()
    parent = build_parent({"trajectory_path": "sub.json"})
    tree.write("sub.json", sub)
    tree.write("t.atif.json", parent)
    parent_size = len(json.dumps(parent))

    loaded = tree.load(
        "t.atif.json", limits=AtifLimits(document_bytes=parent_size + 10)
    )

    assert "subagent_trajectories" not in loaded.document
    (marker,) = loaded.markers
    assert marker.marker.reason == Reason.TOO_LARGE


# --------------------------------------------------------------------------- #
# Subagent references                                                          #
# --------------------------------------------------------------------------- #


def test_a_path_reference_embeds_the_file(tree: Tree):
    summary = "trajectory.summarization-1-summary.json"
    tree.write(f"trial-1/agent/{summary}", build_document())
    tree.write(
        "trial-1/agent/trajectory.json",
        build_parent({"trajectory_path": summary}),
    )

    loaded = tree.load("trial-1/agent/trajectory.json")

    copy = loaded.document["subagent_trajectories"][0]
    assert copy["trajectory_id"] == summary
    assert "extra" not in copy
    assert loaded.markers == ()
    ref = loaded.document["steps"][1]["observation"]["results"][0][
        "subagent_trajectory_ref"
    ][0]
    assert ref == {"trajectory_path": summary, "trajectory_id": summary}

    document, status = ingest(loaded)
    assert status == IngestStatus.INGESTED
    assert (
        "extra"
        not in document["steps"][1]["observation"]["results"][0][
            "subagent_trajectory_ref"
        ][0]
    )


def test_two_path_only_refs_to_the_same_file_share_one_embedded_copy(
    tree: Tree,
):
    tree.write("sub.json", build_document(trajectory_id="qa"))
    tree.write(
        "t.atif.json",
        build_document(
            [
                build_delegating_step({"trajectory_path": "sub.json"}),
                build_delegating_step({"trajectory_path": "sub.json"}),
            ]
        ),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    assert len(loaded.document["subagent_trajectories"]) == 1
    assert loaded.document["subagent_trajectories"][0]["trajectory_id"] == "qa"
    for step in loaded.document["steps"]:
        ref = step["observation"]["results"][0]["subagent_trajectory_ref"][0]
        assert ref == {"trajectory_path": "sub.json", "trajectory_id": "qa"}


@pytest.mark.parametrize("sub_fields", [{"trajectory_id": "qa"}, {}])
def test_id_then_path_only_refs_to_the_same_file_share_one_copy(
    tree: Tree, sub_fields: dict[str, Any]
):
    tree.write("sub.json", build_document(**sub_fields))
    tree.write(
        "t.atif.json",
        build_parent(
            {"trajectory_id": "qa", "trajectory_path": "sub.json"},
            {"trajectory_path": "sub.json"},
        ),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    assert [
        s["trajectory_id"] for s in loaded.document["subagent_trajectories"]
    ] == ["qa"]
    refs = loaded.document["steps"][1]["observation"]["results"][0][
        "subagent_trajectory_ref"
    ]
    assert [ref["trajectory_id"] for ref in refs] == ["qa", "qa"]
    _, status = ingest(loaded)
    assert status == IngestStatus.INGESTED


def test_aliased_path_refs_to_the_same_file_share_one_copy(tree: Tree):
    tree.write("sub.json", build_document(trajectory_id="qa"))
    tree.write(
        "t.atif.json",
        build_parent(
            {"trajectory_path": "sub.json"},
            {"trajectory_path": "./sub.json"},
        ),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    assert [
        s["trajectory_id"] for s in loaded.document["subagent_trajectories"]
    ] == ["qa"]
    _, status = ingest(loaded)
    assert status == IngestStatus.INGESTED


@pytest.mark.parametrize("bad_id", [123, ["qa"]])
def test_non_string_ref_trajectory_id_is_not_overwritten(
    tree: Tree, bad_id: Any
):
    tree.write("sub.json", build_document(trajectory_id="qa"))
    tree.write(
        "t.atif.json",
        build_parent({"trajectory_id": bad_id, "trajectory_path": "sub.json"}),
    )

    loaded = tree.load("t.atif.json")

    assert "subagent_trajectories" not in loaded.document
    with pytest.raises(DocumentRejectedError, match="trajectory_id"):
        validate_document(loaded.document)


@pytest.mark.parametrize("bad_subagents", [123, True])
def test_non_list_subagent_trajectories_reaches_schema_validation(
    tree: Tree, bad_subagents: Any
):
    tree.write(
        "t.atif.json", build_document(subagent_trajectories=bad_subagents)
    )

    loaded = tree.load("t.atif.json")

    assert loaded.source_updated_at is not None
    with pytest.raises(DocumentRejectedError, match="valid list"):
        validate_document(loaded.document)


def test_an_embedded_copy_keeps_its_own_trajectory_id(tree: Tree):
    tree.write("sub.json", build_document(trajectory_id="qa"))
    tree.write("t.atif.json", build_parent({"trajectory_path": "sub.json"}))

    loaded = tree.load("t.atif.json")

    assert loaded.document["subagent_trajectories"][0]["trajectory_id"] == "qa"
    _, status = ingest(loaded)
    assert status == IngestStatus.INGESTED


def test_a_copy_without_an_id_takes_the_references_id(tree: Tree):
    tree.write("sub.json", build_document())
    tree.write(
        "t.atif.json",
        build_parent({"trajectory_id": "qa", "trajectory_path": "sub.json"}),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.document["subagent_trajectories"][0]["trajectory_id"] == "qa"
    document, _ = ingest(loaded)
    ref = document["steps"][1]["observation"]["results"][0][
        "subagent_trajectory_ref"
    ][0]
    assert "extra" not in ref


def test_a_reference_matching_an_embedded_subagent_reads_nothing(tree: Tree):
    embedded = build_document(trajectory_id="qa")
    tree.write(
        "t.atif.json",
        build_parent(
            {"trajectory_id": "qa", "trajectory_path": "absent.json"},
            subagent_trajectories=[embedded],
        ),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    assert len(loaded.document["subagent_trajectories"]) == 1
    assert [name for name, _ in tree.client.download_requests] in (
        [],
        [f"{PREFIX}/t.atif.json"],
    )


def test_a_session_id_only_reference_is_left_to_annotation(tree: Tree):
    document = build_parent({"session_id": "s-7"}, schema_version="ATIF-v1.6")
    tree.write("t.atif.json", document)

    loaded = tree.load("t.atif.json")

    assert loaded.markers == ()
    stored, status = ingest(loaded)
    assert status == IngestStatus.INGESTED
    ref = stored["steps"][1]["observation"]["results"][0][
        "subagent_trajectory_ref"
    ][0]
    assert ref["extra"]["aqua"]["missing"] == [
        {"path": "trajectory", "reason": "not_recorded"}
    ]


@pytest.mark.parametrize(
    ("trajectory_path", "content", "reason", "detail"),
    [
        ("absent.json", None, Reason.READ_ERROR, "404"),
        ("../../x.json", None, Reason.PATH_TRAVERSAL, None),
        ("https://host/sub.json", None, Reason.OUT_OF_SCOPE, None),
        ("s3://bucket/sub.json", None, Reason.OUT_OF_SCOPE, None),
        ("sub.json", b"{not json", Reason.INVALID_CONTENT, "invalid_json"),
        ("sub.json", b'{"steps": "x"}', Reason.INVALID_CONTENT, "invalid_atif"),
        (
            "sub.json",
            json.dumps(
                build_document(extra={"aqua": {"missing": []}})
            ).encode(),
            Reason.INVALID_CONTENT,
            "invalid_atif",
        ),
        ("", None, Reason.INVALID_CONTENT, "empty_path"),
    ],
)
def test_an_unresolved_subagent_gets_a_trajectory_marker(
    tree: Tree,
    trajectory_path: str,
    content: bytes | None,
    reason: Reason,
    detail: str | None,
):
    if content is not None:
        tree.write("sub.json", content)
    tree.write(
        "t.atif.json", build_parent({"trajectory_path": trajectory_path})
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (build_ref_marker(reason, detail),)
    assert "subagent_trajectories" not in loaded.document
    _, status = ingest(loaded)
    expected = (
        IngestStatus.INGESTED
        if reason == Reason.OUT_OF_SCOPE
        else IngestStatus.PARTIAL
    )
    assert status == expected


def test_a_reference_cycle_is_cut(tree: Tree):
    tree.write("t.atif.json", build_parent({"trajectory_path": "a.json"}))
    tree.write("a.json", build_parent({"trajectory_path": "t.atif.json"}))

    loaded = tree.load("t.atif.json")

    assert len(loaded.document["subagent_trajectories"]) == 1
    assert (
        loaded.document["subagent_trajectories"][0]["trajectory_id"] == "a.json"
    )
    nested_ref = ("subagent_trajectories", 0, *REF)
    assert loaded.markers == (
        build_ref_marker(Reason.INVALID_CONTENT, "cycle", ref=nested_ref),
    )


def test_nested_references_stop_at_the_depth_limit(tree: Tree):
    for level in range(5):
        tree.write(
            f"level-{level}.json",
            build_parent({"trajectory_path": f"level-{level + 1}.json"}),
        )
    tree.write("level-5.json", build_document())
    tree.write("t.atif.json", build_parent({"trajectory_path": "level-0.json"}))

    loaded = tree.load("t.atif.json", limits=AtifLimits(subagent_nesting=3))

    copy = ("subagent_trajectories", 0)
    level0 = loaded.document["subagent_trajectories"][0]
    level1 = level0["subagent_trajectories"][0]
    level2 = level1["subagent_trajectories"][0]
    assert [
        level0["trajectory_id"],
        level1["trajectory_id"],
        level2["trajectory_id"],
    ] == ["level-0.json", "level-1.json", "level-2.json"]
    assert "subagent_trajectories" not in level2
    assert loaded.markers == (
        build_ref_marker(Reason.TOO_LARGE, "3", ref=(*copy * 3, *REF)),
    )
    _, status = ingest(loaded)
    assert status == IngestStatus.PARTIAL


def test_a_nested_file_resolves_paths_from_its_own_directory(tree: Tree):
    tree.write(
        "subagents/qa.json",
        build_document(
            [
                build_image_step("shot.png"),
                build_delegating_step({"trajectory_path": "inner/deep.json"}),
            ]
        ),
    )
    tree.write("subagents/shot.png", PNG)
    tree.write("subagents/inner/deep.json", build_document())
    tree.write(
        "t.atif.json", build_parent({"trajectory_path": "subagents/qa.json"})
    )

    loaded = tree.load("t.atif.json")

    copy = ("subagent_trajectories", 0)
    assert loaded.markers == ()
    assert (
        loaded.document["subagent_trajectories"][0]["trajectory_id"]
        == "subagents/qa.json"
    )
    assert read_file_key(loaded, (*copy, *STEP_0, "message", 1)) == (
        "subagents/shot.png"
    )
    inner = loaded.document["subagent_trajectories"][0][
        "subagent_trajectories"
    ][0]
    assert inner["trajectory_id"] == "inner/deep.json"
    _, status = ingest(loaded)
    assert status == IngestStatus.INGESTED


def test_an_id_already_in_the_list_is_not_embedded_twice(tree: Tree):
    tree.write("sub.json", build_document(trajectory_id="qa"))
    tree.write(
        "t.atif.json",
        build_parent(
            {"trajectory_path": "sub.json"},
            subagent_trajectories=[build_document(trajectory_id="qa")],
        ),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (
        build_ref_marker(Reason.INVALID_CONTENT, "duplicate_trajectory_id"),
    )
    _, status = ingest(loaded)
    assert status == IngestStatus.PARTIAL


def test_a_file_whose_id_differs_from_the_reference_is_not_embedded(tree: Tree):
    tree.write("sub.json", build_document(trajectory_id="other"))
    tree.write(
        "t.atif.json",
        build_parent({"trajectory_id": "qa", "trajectory_path": "sub.json"}),
    )

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (
        build_ref_marker(Reason.INVALID_CONTENT, "trajectory_id_mismatch"),
    )


def test_a_root_file_with_unknown_extra_aqua_on_a_subagent_is_rejected(
    tree: Tree,
):
    forged = build_document(trajectory_id="forged")
    forged["extra"] = {"aqua": {"unknown_key": "value"}}
    tree.write("sub.json", build_document())
    tree.write(
        "t.atif.json",
        build_parent(
            {"trajectory_path": "sub.json"}, subagent_trajectories=[forged]
        ),
    )

    loaded = tree.load("t.atif.json")

    with pytest.raises(DocumentRejectedError) as raised:
        ingest(loaded)
    assert raised.value.reason == (
        "subagent_trajectories.0.extra.aqua.unknown_key: "
        "Extra inputs are not permitted"
    )


def test_file_part_bridge_attachments_resolve_and_mark_failures(tree: Tree):
    tree.write("docs/invoice.pdf", b"%PDF-1.7")
    doc = build_document(
        steps=[
            {
                "step_id": 1,
                "source": "user",
                "message": "",
                "extra": {
                    "aqua": {
                        "message": [
                            {"type": "text", "text": "Check these."},
                            {
                                "type": "file",
                                "source": {
                                    "media_type": "application/pdf",
                                    "path": "docs/invoice.pdf",
                                },
                            },
                            {
                                "type": "file",
                                "source": {
                                    "media_type": "video/mp4",
                                    "path": "docs/missing.mp4",
                                },
                            },
                        ]
                    }
                },
            }
        ]
    )
    tree.write("t.atif.json", doc)

    source = tree.build_source()
    (loaded,) = source.load_trajectories(["t.atif.json"])
    assert isinstance(loaded, LoadedDocument)

    pdf_path = ("steps", 0, "extra", "aqua", "message", 1)
    video_path = ("steps", 0, "extra", "aqua", "message", 2)
    location = loaded.locations[pdf_path]
    assert isinstance(location, ExternalAttachment)
    assert source.read_attachment(location) == b"%PDF-1.7"
    assert video_path not in loaded.locations
    assert loaded.markers == (
        PendingMissingMarker(
            ("steps", 0),
            MissingMarker("extra.aqua.message[2]", Reason.READ_ERROR, "404"),
        ),
    )


def test_markers_of_an_embedded_file_are_placed_inside_the_copy(tree: Tree):
    tree.write(
        "sub.json",
        b'{"schema_version": "ATIF-v1.8", "agent": {"name": "a", "version": "1"}, "steps": [{"step_id": 1, "source": "user", "message": "\\udc00"}]}',
    )
    tree.write("t.atif.json", build_parent({"trajectory_path": "sub.json"}))

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (
        PendingMissingMarker(
            ("subagent_trajectories", 0, "steps", 0),
            MissingMarker("message", Reason.INVALID_CONTENT, "lone_surrogate"),
        ),
    )


def test_an_embedded_copy_stays_within_the_json_depth(tree: Tree):
    deep: Any = "x"
    for _ in range(253):
        deep = [deep]
    sub = build_document()
    sub["extra"] = {"deep": deep}
    tree.write("sub.json", sub)
    tree.write("t.atif.json", build_parent({"trajectory_path": "sub.json"}))

    loaded = tree.load("t.atif.json")

    assert loaded.markers == (
        build_ref_marker(Reason.INVALID_CONTENT, "invalid_json"),
    )


# --------------------------------------------------------------------------- #
# Attachments, construction                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("file_id", "ref_id", "copy_id"),
    [
        ("", None, "sub.json"),
        (None, "", "sub.json"),
        ("", "", "sub.json"),
        ("", "from-ref", "from-ref"),
        ("from-file", "", "from-file"),
    ],
)
def test_an_empty_subagent_id_counts_as_none(
    tree: Tree, file_id: str | None, ref_id: str | None, copy_id: str
):
    subagent = build_document()
    if file_id is not None:
        subagent["trajectory_id"] = file_id
    tree.write("sub.json", subagent)
    ref: dict[str, Any] = {"trajectory_path": "sub.json"}
    if ref_id is not None:
        ref["trajectory_id"] = ref_id
    tree.write(
        "t.atif.json", build_document([build_delegating_step(ref, step_id=1)])
    )
    (loaded,) = tree.build_source().load_trajectories(["t.atif.json"])
    assert isinstance(loaded, LoadedDocument)

    (copy,) = loaded.document["subagent_trajectories"]
    assert copy["trajectory_id"] == copy_id
    assert loaded.markers == ()
    validated = validate_document(loaded.document)
    annotate_document(
        loaded.document,
        markers=validated.markers,
    )
    step = loaded.document["steps"][-1]
    reference = step["observation"]["results"][0]["subagent_trajectory_ref"][0]
    assert "extra" not in reference


def test_read_attachment_reads_a_resolved_file(tree: Tree):
    tree.write("shot.png", PNG)
    tree.write("t.atif.json", build_document([build_image_step("shot.png")]))
    source = tree.build_source()
    (loaded,) = source.load_trajectories(["t.atif.json"])
    assert isinstance(loaded, LoadedDocument)

    location = loaded.locations[(*STEP_0, "message", 1)]

    assert isinstance(location, ExternalAttachment)
    assert source.read_attachment(location) == PNG


@pytest.mark.parametrize("uri", ["s3://bucket/x", "gs://", "gs:///x"])
def test_an_unsupported_uri_is_refused(uri: str):
    with pytest.raises(ValueError, match="is not valid"):
        create_atif_files(uri, client_factory=FakeGcsClient)


def test_the_file_source_is_a_trajectory_source(tree: Tree):
    source: TrajectorySource = tree.build_source()

    assert list(source.load_trajectories([])) == []


def test_a_root_is_read_when_the_consumer_asks_for_it(gcs: Tree):
    gcs.write("t.atif.json", build_document())
    gcs.write("u.atif.json", build_document())
    downloads = gcs.client.download_requests

    documents = gcs.build_source().load_trajectories(
        ["t.atif.json", "gone.atif.json", "u.atif.json"]
    )

    assert downloads == []
    assert isinstance(next(documents), LoadedDocument)
    assert [name for name, _ in downloads] == [f"{PREFIX}/t.atif.json"]
    assert isinstance(next(documents), FailedDocument)
    assert [name for name, _ in downloads] == [f"{PREFIX}/t.atif.json"]
    assert next(documents).key == "u.atif.json"
    assert len(downloads) == 2
    assert next(documents, None) is None


# --------------------------------------------------------------------------- #
# One document or reference never fails the batch                              #
# --------------------------------------------------------------------------- #


def build_subagent_ref_document(path: str) -> dict[str, Any]:
    return build_parent({"trajectory_path": path})


@pytest.mark.parametrize(
    "document",
    [
        build_document([build_image_step("a\x00b.png")]),
        build_document([build_image_step("/abs\x00/x.png")]),
        build_document([build_image_step(f"gs://{BUCKET}/{PREFIX}/a\x00b")]),
        build_subagent_ref_document("sub\x00.json"),
        build_subagent_ref_document("/abs\x00.json"),
    ],
    ids=["relative", "absolute", "gcs", "subagent", "subagent-absolute"],
)
def test_a_nul_character_in_a_path_is_marked_not_raised(
    tree: Tree, document: dict[str, Any]
):
    tree.write("t.atif.json", document)
    tree.write("u.atif.json", build_document())

    first, second = tree.build_source().load_trajectories(
        ["t.atif.json", "u.atif.json"]
    )

    assert isinstance(first, LoadedDocument)
    (marker,) = first.markers
    assert (marker.marker.reason, marker.marker.detail) == (
        Reason.INVALID_CONTENT,
        "nul_character",
    )
    assert isinstance(second, LoadedDocument)


def test_a_percent_encoded_data_url_is_measured_without_decoding():
    url = "data:image/png," + "%41" * 3_000_000

    tracemalloc.start()
    try:
        with pytest.raises(AttachmentUnreadableError) as over:
            check_data_url(url, 1024)
        within = check_data_url(url, 3_000_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (over.value.reason, over.value.detail) == (
        Reason.TOO_LARGE,
        "3000000",
    )
    assert within == InlineAttachment(uri=url, size=3_000_000)
    assert peak < 1_000_000
    for bad in ("data:,a%4", "data:,%zz", "data:,caf\u00e9"):
        with pytest.raises(AttachmentUnreadableError) as raised:
            check_data_url(bad, 10)
        assert (raised.value.reason, raised.value.detail) == (
            Reason.INVALID_CONTENT,
            "data_url",
        )


def test_decoding_a_percent_encoded_data_url_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(trajectory_source, "_UNQUOTE_CHUNK", 30_000)
    url = "data:image/png," + "%41" * 300_000

    tracemalloc.start()
    try:
        decoded = decode_inline_attachment(
            InlineAttachment(uri=url, size=300_000), 300_000
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert decoded == b"A" * 300_000
    # Decoding the payload at once holds one object per escape: over 10 MB.
    assert peak < 4_000_000
    with pytest.raises(AttachmentUnreadableError) as raised:
        decode_inline_attachment(InlineAttachment(uri=url, size=300_000), 10)
    assert raised.value.reason == Reason.TOO_LARGE


@pytest.mark.parametrize("payload", ["a%41b%42", "%41%42%43%44"])
def test_chunked_decoding_never_splits_an_escape(
    monkeypatch: pytest.MonkeyPatch, payload: str
):
    monkeypatch.setattr(trajectory_source, "_UNQUOTE_CHUNK", 3)
    attachment = check_data_url(f"data:,{payload}", 100)

    assert decode_inline_attachment(attachment, 100) == unquote_to_bytes(
        payload
    )


@pytest.mark.parametrize("payload", ["abc%2", "x%41%4"])
def test_chunked_decoding_rejects_truncated_escape(payload: str):
    with pytest.raises(AttachmentUnreadableError) as raised:
        check_data_url(f"data:,{payload}", 100)
    assert (raised.value.reason, raised.value.detail) == (
        Reason.INVALID_CONTENT,
        "data_url",
    )


def test_a_base64_data_url_decodes_to_its_bytes():
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()

    assert (
        decode_inline_attachment(InlineAttachment(uri=url, size=len(PNG)), 100)
        == PNG
    )
    with pytest.raises(AttachmentUnreadableError) as raised:
        check_data_url("data:;base64,QQ", 10)
    assert (raised.value.reason, raised.value.detail) == (
        Reason.INVALID_CONTENT,
        "data_url",
    )
    assert check_data_url("data:;base64,QQ==", 10) == InlineAttachment(
        uri="data:;base64,QQ==", size=1
    )


def run_with_timeout(function: Any, seconds: float = 5) -> Any:
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("value", function()), daemon=True
    )
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "hung"
    return result["value"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_a_fifo_subagent_file_does_not_hang(local: Tree):
    os.mkfifo(local.root / "sub.json")
    local.write("t.atif.json", build_parent({"trajectory_path": "sub.json"}))

    loaded = run_with_timeout(lambda: local.load("t.atif.json"))

    assert loaded.markers == (build_ref_marker(Reason.READ_ERROR, "404"),)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_a_root_replaced_by_a_fifo_does_not_hang(local: Tree):
    local.write("t.atif.json", build_document())
    source = local.build_source()
    assert [
        item.key for item in source.list_trajectory_keys(START, END, 5)
    ] == ["t.atif.json"]
    (local.root / "t.atif.json").unlink()
    os.mkfifo(local.root / "t.atif.json")

    (failed,) = run_with_timeout(
        lambda: list(source.load_trajectories(["t.atif.json"]))
    )

    assert isinstance(failed, FailedDocument)
    assert failed.reason == "read_error 404"


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("reset"),
        auth_exceptions.TransportError("no route"),
        auth_exceptions.RefreshError("expired"),
        TimeoutError("slow"),
    ],
    ids=["connection", "transport", "refresh", "timeout"],
)
def test_a_transport_error_fails_only_its_document(gcs: Tree, error: Exception):
    gcs.write("t.atif.json", build_document())
    gcs.write("u.atif.json", build_document())
    gcs.client.errors[(BUCKET, f"{PREFIX}/t.atif.json")] = error

    first, second = gcs.build_source().load_trajectories(
        ["t.atif.json", "u.atif.json"]
    )

    assert isinstance(first, FailedDocument)
    assert first.reason == "read_error"
    assert isinstance(second, LoadedDocument)


def test_a_transport_error_on_a_reference_is_a_marker(gcs: Tree):
    gcs.write("shot.png", PNG)
    gcs.write("sub.json", build_document())
    document = build_parent({"trajectory_path": "sub.json"})
    document["steps"][0] = build_image_step("shot.png")
    gcs.write("t.atif.json", document)
    gcs.client.errors[(BUCKET, f"{PREFIX}/shot.png")] = ConnectionError("x")
    gcs.client.errors[(BUCKET, f"{PREFIX}/sub.json")] = ConnectionError("x")

    loaded = gcs.load("t.atif.json")

    assert loaded.markers == (
        build_part_marker(Reason.READ_ERROR),
        build_ref_marker(Reason.READ_ERROR),
    )


def test_a_failed_download_is_a_read_error(gcs: Tree):
    gcs.write("t.atif.json", build_document())
    gcs.client.download_errors[(BUCKET, f"{PREFIX}/t.atif.json")] = (
        ConnectionError("dropped")
    )

    assert gcs.load_failure("t.atif.json").reason == "read_error"


def test_an_unexpected_error_fails_only_its_document(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
):
    tree.write("t.atif.json", build_document())
    tree.write("u.atif.json", build_document())

    def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(atif_references.DocumentResolver, "resolve", explode)
    first, second = tree.build_source().load_trajectories(
        ["t.atif.json", "u.atif.json"]
    )

    assert [
        (failed.key, failed.reason)
        for failed in (first, second)
        if isinstance(failed, FailedDocument)
    ] == [
        ("t.atif.json", "could not load the document"),
        ("u.atif.json", "could not load the document"),
    ]


# --------------------------------------------------------------------------- #
# Path security                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("use_proc_fd", [True, False])
def test_a_link_swapped_in_after_the_check_is_refused(
    local: Tree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_proc_fd: bool,
):
    if use_proc_fd and not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc to see the opened file")
    if not use_proc_fd:
        monkeypatch.setattr(atif_files, "_PROC_FD", tmp_path / "no-proc")
    (local.root / "sub").mkdir()
    (local.root / "sub" / "s.json").write_text("{}")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "s.json").write_text('{"secret": true}')
    files = create_atif_files(str(local.root))
    real = os.path.realpath

    def swapping_realpath(path: Any, *args: Any, **kwargs: Any) -> str:
        resolved = real(path, *args, **kwargs)
        if (
            str(path).endswith("sub/s.json")
            and not (local.root / "sub").is_symlink()
        ):
            shutil.rmtree(local.root / "sub")
            os.symlink(tmp_path / "secret", local.root / "sub")
        return resolved

    monkeypatch.setattr(atif_files.os.path, "realpath", swapping_realpath)

    with pytest.raises(atif_files.PathOutsidePrefixError):
        files.read_object("sub/s.json", 1000)


def test_paths_from_a_document_are_not_logged(
    tree: Tree, caplog: pytest.LogCaptureFixture
):
    tree.write(
        "t.atif.json",
        build_document([build_image_step("x\nFORGED user@example.com.png")]),
    )
    tree.write(
        "u.atif.json",
        build_parent({"trajectory_path": "y\nFORGED user@example.com.json"}),
    )
    # A subagent file that validation rejects for a key it does not know.
    rejected = build_document()
    rejected["steps"][0]["z\nFORGED user@example.com"] = 1
    tree.write("sub.json", rejected)
    tree.write("v.atif.json", build_parent({"trajectory_path": "sub.json"}))

    with caplog.at_level(logging.DEBUG):
        list(
            tree.build_source().load_trajectories(
                ["t.atif.json", "u.atif.json", "v.atif.json"]
            )
        )

    assert caplog.records
    assert "FORGED" not in caplog.text
    assert "example.com" not in caplog.text


def test_an_upper_case_gcs_scheme_is_matched(gcs: Tree):
    gcs.write("shot.png", PNG)
    gcs.write(
        "t.atif.json",
        build_document(
            [
                build_image_step(
                    f"GS://{BUCKET}/{PREFIX}/shot.png",
                    f"Gs://{BUCKET}/{PREFIX}/shot.png",
                )
            ]
        ),
    )

    loaded = gcs.load("t.atif.json")

    assert loaded.markers == ()
    assert read_file_key(loaded, (*STEP_0, "message", 1)) == "shot.png"


@pytest.mark.parametrize(
    "path",
    [
        "gs://other-bucket/x.json",
        f"gs://{BUCKET}/{PREFIX}-private/x.json",
        f"gs://{BUCKET}/{PREFIX}/../x.json",
    ],
)
def test_a_subagent_gcs_url_outside_the_prefix_is_refused(gcs: Tree, path: str):
    gcs.client.contents[(BUCKET, f"{PREFIX}-private/x.json")] = json.dumps(
        build_document()
    ).encode()
    gcs.write("t.atif.json", build_parent({"trajectory_path": path}))

    loaded = gcs.load("t.atif.json")

    assert loaded.markers == (build_ref_marker(Reason.PATH_TRAVERSAL),)


def test_a_subagent_file_linked_outside_is_refused(local: Tree, tmp_path: Path):
    (tmp_path / "sub.json").write_text(json.dumps(build_document()))
    (local.root / "sub.json").symlink_to(tmp_path / "sub.json")
    local.write("t.atif.json", build_parent({"trajectory_path": "sub.json"}))

    loaded = local.load("t.atif.json")

    assert loaded.markers == (build_ref_marker(Reason.PATH_TRAVERSAL),)


def test_an_absolute_path_into_a_sibling_directory_is_refused(
    local: Tree, tmp_path: Path
):
    sibling = tmp_path / "runs-private"
    sibling.mkdir()
    (sibling / "x.png").write_bytes(PNG)
    local.write(
        "t.atif.json",
        build_document(
            [
                build_image_step(
                    str(sibling / "x.png"),
                    "../runs-private/x.png",
                    f"{local.root}/../runs-private/x.png",
                )
            ]
        ),
    )

    loaded = local.load("t.atif.json")

    assert [marker.marker.reason for marker in loaded.markers] == [
        Reason.PATH_TRAVERSAL
    ] * 3


def test_a_dangling_link_is_refused(local: Tree, tmp_path: Path):
    (local.root / "gone.png").symlink_to(tmp_path / "missing.png")
    (local.root / "loop.png").symlink_to(local.root / "loop.png")
    local.write(
        "t.atif.json",
        build_document([build_image_step("gone.png", "loop.png")]),
    )

    loaded = local.load("t.atif.json")

    assert loaded.markers[0] == build_part_marker(Reason.PATH_TRAVERSAL)
    assert loaded.markers[1].marker.reason in (
        Reason.READ_ERROR,
        Reason.PATH_TRAVERSAL,
    )


def test_a_symbolic_link_as_the_attached_directory_works(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "shot.png").write_bytes(PNG)
    (real / "sub.json").write_text(json.dumps(build_document()))
    document = build_parent({"trajectory_path": "sub.json"})
    document["steps"][0] = build_image_step("shot.png")
    (real / "t.atif.json").write_text(json.dumps(document))
    (tmp_path / "link").symlink_to(real)

    (loaded,) = AtifFileSource(str(tmp_path / "link")).load_trajectories(
        ["t.atif.json"]
    )

    assert isinstance(loaded, LoadedDocument)
    assert loaded.markers == ()
    assert (
        loaded.document["subagent_trajectories"][0]["trajectory_id"]
        == "sub.json"
    )
    assert read_file_key(loaded, (*STEP_0, "message", 1)) == "shot.png"


def test_a_linked_root_resolves_paths_from_where_it_is_listed(local: Tree):
    (local.root / "store").mkdir()
    (local.root / "store" / "real.json").write_text(
        json.dumps(build_document([build_image_step("images/a.png")]))
    )
    (local.root / "trial").mkdir()
    (local.root / "trial" / "trajectory.json").symlink_to(
        local.root / "store" / "real.json"
    )
    local.write("trial/images/a.png", PNG)

    loaded = local.load("trial/trajectory.json")

    assert loaded.markers == ()
    assert (
        read_file_key(loaded, (*STEP_0, "message", 1)) == "trial/images/a.png"
    )


def test_a_compressed_object_is_never_decompressed(gcs: Tree):
    gcs.write(
        "t.atif.json", gzip.compress(json.dumps(build_document()).encode())
    )
    gcs.client.content_encodings[(BUCKET, f"{PREFIX}/t.atif.json")] = "gzip"

    assert gcs.load_failure("t.atif.json").reason == "invalid JSON"


# --------------------------------------------------------------------------- #
# Configuration and listing                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri",
    [
        f"gs://{BUCKET}/agent-a//runs",
        f"gs://{BUCKET}/agent-a/./runs",
        f"gs://{BUCKET}/../runs",
        "",
    ],
)
def test_a_prefix_with_odd_segments_is_refused(uri: str):
    with pytest.raises(ValueError, match="is not valid"):
        create_atif_files(uri, client_factory=FakeGcsClient)


@pytest.mark.parametrize(
    "pattern", ["[z-a].json", "[[:alpha:]]*.json", "**/**/**/*.json"]
)
def test_an_invalid_glob_is_a_value_error(pattern: str, tmp_path: Path):
    with pytest.raises(ValueError, match="atif_glob"):
        AtifFileSource(str(tmp_path), atif_glob=pattern)


def test_glob_character_classes_stay_within_one_segment():
    assert not build_glob_pattern("a[!b]c/t.json").fullmatch("a/c/t.json")
    assert build_glob_pattern("a[!b]c/t.json").fullmatch("axc/t.json")
    assert not build_glob_pattern("run-[!0-9]*.atif.json").fullmatch(
        "run-/sub.atif.json"
    )
    negated_bracket = build_glob_pattern("[!]]x.json")
    assert negated_bracket.fullmatch("ax.json")
    assert not negated_bracket.fullmatch("]x.json")
    assert not negated_bracket.fullmatch("/x.json")


def test_reading_an_empty_attachment_succeeds(tree: Tree):
    tree.write("empty.png", b"")
    tree.write("t.atif.json", build_document([build_image_step("empty.png")]))
    source = tree.build_source()
    (loaded,) = source.load_trajectories(["t.atif.json"])
    assert isinstance(loaded, LoadedDocument)
    attachment = loaded.locations[(*STEP_0, "message", 1)]
    assert isinstance(attachment, ExternalAttachment)
    assert source.read_attachment(attachment) == b""


def test_a_missing_directory_is_an_error_not_an_empty_window(tmp_path: Path):
    source = AtifFileSource(str(tmp_path / "typo"))

    with pytest.raises(ObjectReadError) as raised:
        source.count_trajectories(START, END)
    assert raised.value.status == "404"


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs permissions"
)
def test_an_unreadable_directory_is_an_error(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    root.chmod(0)
    try:
        with pytest.raises(ObjectReadError) as raised:
            AtifFileSource(str(root)).count_trajectories(START, END)
    finally:
        root.chmod(0o700)
    assert raised.value.status == "403"


def test_a_denied_bucket_listing_is_an_error(gcs: Tree):
    gcs.client.list_error = api_exceptions.Forbidden("denied")

    with pytest.raises(ObjectReadError) as raised:
        gcs.build_source().count_trajectories(START, END)
    assert raised.value.status == "403"


def test_counting_then_listing_a_window_lists_once(gcs: Tree):
    gcs.write("t.atif.json", build_document())
    source = gcs.build_source()

    assert source.count_trajectories(START, END).count == 1
    assert [
        item.key for item in source.list_trajectory_keys(START, END, 5)
    ] == ["t.atif.json"]
    assert gcs.client.listings == 1
    assert [
        item.key for item in source.list_trajectory_keys(START, END, 5)
    ] == ["t.atif.json"]
    source.count_trajectories(START, END)
    source.list_trajectory_keys(START, START, 5)
    assert gcs.client.listings == 4


def test_the_same_window_in_another_form_lists_once(gcs: Tree):
    gcs.write("t.atif.json", build_document())
    source = gcs.build_source()
    paris = dt.timezone(dt.timedelta(hours=2))

    source.count_trajectories(START, END)
    keys = source.list_trajectory_keys(
        START.replace(tzinfo=None), END.astimezone(paris), 5
    )

    assert [item.key for item in keys] == ["t.atif.json"]
    assert gcs.client.listings == 1


def test_a_listing_cut_short_says_so_in_the_count(tree: Tree):
    for number in range(4):
        tree.write(f"t-{number}.atif.json", build_document())

    truncated_source = tree.build_source(max_listed_objects=3)
    cut = truncated_source.count_trajectories(START, END)
    cut_sample = truncated_source.list_trajectory_keys(START, END, 2)
    direct_cut_sample = tree.build_source(
        max_listed_objects=3
    ).list_trajectory_keys(START, END, 2)
    whole_source = tree.build_source(max_listed_objects=4)
    whole = whole_source.count_trajectories(START, END)
    whole_sample = whole_source.list_trajectory_keys(START, END, 2)

    assert cut == WindowCount(count=3, truncated=True)
    assert len(cut_sample) == 2
    assert all(item.sampling is None for item in cut_sample)
    assert len(direct_cut_sample) == 2
    assert all(item.sampling is None for item in direct_cut_sample)
    assert whole == WindowCount(count=4, truncated=False)
    assert len(whole_sample) == 2
    assert all(
        item.sampling == SamplingRecord(inclusion_probability=0.5)
        for item in whole_sample
    )


def test_default_atif_limits():
    limits = AtifLimits()

    assert limits.document_bytes == 256 * 1024 * 1024
    assert limits.attachment_bytes == 64 * 1024 * 1024
    assert limits.references == 1000
    assert limits.subagent_nesting == 8
    assert atif_references.MAX_JSON_DEPTH == 256


# --------------------------------------------------------------------------- #
# Reading attachments                                                          #
# --------------------------------------------------------------------------- #


def test_a_loaded_data_url_decodes_without_the_source(tree: Tree):
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    tree.write("t.atif.json", build_document([build_image_step(url)]))
    loaded = tree.load("t.atif.json")

    location = loaded.locations[(*STEP_0, "message", 1)]

    assert isinstance(location, InlineAttachment)
    assert decode_inline_attachment(location, len(PNG)) == PNG


def test_read_attachment_reports_why_a_file_cannot_be_read(tree: Tree):
    tree.write("shot.png", PNG)
    tree.write("t.atif.json", build_document([build_image_step("shot.png")]))
    source = tree.build_source(limits=AtifLimits(attachment_bytes=len(PNG)))
    (loaded,) = source.load_trajectories(["t.atif.json"])
    assert isinstance(loaded, LoadedDocument)
    location = loaded.locations[(*STEP_0, "message", 1)]
    assert isinstance(location, ExternalAttachment)
    tree.write("shot.png", PNG + b"more")

    with pytest.raises(AttachmentUnreadableError) as too_large:
        source.read_attachment(location)
    if tree.kind == "gcs":
        del tree.client.contents[(BUCKET, f"{PREFIX}/shot.png")]
    else:
        (tree.root / "shot.png").unlink()
    with pytest.raises(AttachmentUnreadableError) as missing:
        source.read_attachment(location)

    assert too_large.value.reason == Reason.TOO_LARGE
    assert (missing.value.reason, missing.value.detail) == (
        Reason.READ_ERROR,
        "404",
    )
