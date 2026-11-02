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

"""The goal and the memories: storage, ids, and the route-facing payloads.

A fake GCS client stands in for the bucket, behind the deployed `GcsObjectStore`
-- the point of these is the object layout and the semantics on top of it, not
the client library. `test_object_store.py` holds the standalone
`FileObjectStore` to the same answers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import pytest
from ambient_quality_agent.tools.documents import goal as goal_doc
from ambient_quality_agent.tools.documents import memories as memories_doc
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.objects.store import StorageNotConfiguredError

BUCKET = "aqa-jobs"


class FakeNotFound(Exception):
    pass


class _Blob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def download_as_text(self) -> str:
        if self.name not in self._store:
            raise _api_exceptions().NotFound(self.name)
        return self._store[self.name].decode("utf-8")

    def upload_from_string(
        self,
        data: bytes | str,
        if_generation_match: int | None = None,
        content_type: str | None = None,
    ) -> None:
        # Only the "must not exist" precondition (0) is modelled.
        if if_generation_match == 0 and self.name in self._store:
            raise _api_exceptions().PreconditionFailed(self.name)
        self._store[self.name] = (
            data if isinstance(data, bytes) else data.encode()
        )

    def delete(self) -> None:
        if self.name not in self._store:
            raise _api_exceptions().NotFound(self.name)
        del self._store[self.name]


class _Bucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, name: str) -> _Blob:
        return _Blob(self._store, name)


class FakeClient:
    """Enough of `storage.Client` for these three operations."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def bucket(self, name: str) -> _Bucket:
        return _Bucket(self.objects)

    def list_blobs(self, bucket: str, prefix: str = "") -> list[Any]:
        return [
            _Blob(self.objects, name)
            for name in sorted(self.objects)
            if name.startswith(prefix)
        ]


def _api_exceptions() -> Any:
    from google.api_core import exceptions

    return exceptions


@pytest.fixture
def gcs() -> FakeClient:
    return FakeClient()


@pytest.fixture
def store(gcs: FakeClient) -> GcsObjectStore:
    return GcsObjectStore(store, client_factory=lambda: gcs)


# --------------------------------------------------------------------------- #
# The goal                                                                     #
# --------------------------------------------------------------------------- #


def test_a_goal_round_trips(gcs, store) -> None:
    assert goal_doc.read_goal(store) is None

    goal_doc.save_goal(store, "  Book travel and file expenses.\n")

    assert goal_doc.read_goal(store) == "Book travel and file expenses."
    assert gcs.objects["goal.md"].decode() == "Book travel and file expenses."


def _goal_objects(gcs: FakeClient, prefix: str) -> dict[str, dict]:
    return {
        name: json.loads(data)
        for name, data in gcs.objects.items()
        if name.startswith(prefix)
    }


@pytest.mark.parametrize("empty", ["", "   ", "\n\t "])
def test_saving_an_empty_goal_removes_it_and_keeps_its_version(
    gcs, empty, store
) -> None:
    """No goal is a state the developer can choose. The removed text stays a
    version, so it can be restored, and the removal is logged as an activation
    of no version."""
    goal_doc.save_goal(store, "The real goal.")

    assert goal_doc.save_goal(store, empty) == ""

    assert goal_doc.read_goal(store) is None
    assert (
        goal_doc.load_investigation_goal(store) == goal_doc.InvestigationGoal()
    )
    [kept] = goal_doc.list_versions(store)
    assert kept.text == "The real goal."
    activations = _goal_objects(gcs, "goals/activations/")
    [removal] = [n for n in activations if n.endswith("-none.json")]
    assert activations[removal]["version"] is None


def test_a_removed_goal_can_be_restored(store) -> None:
    goal_doc.save_goal(store, "The real goal.")
    goal_doc.save_goal(store, "")

    goal_doc.save_goal(store, "The real goal.")

    assert goal_doc.read_goal(store) == "The real goal."
    assert len(goal_doc.list_versions(store)) == 1


def test_saving_a_goal_stores_a_version_and_an_activation(gcs, store) -> None:
    """Every save keeps the text as an immutable version and logs that it was
    made active, so an earlier goal is never lost to an overwrite."""
    goal_doc.save_goal(store, "  Book travel and file expenses.\n")

    version = goal_doc.compute_goal_version("Book travel and file expenses.")
    versions = _goal_objects(gcs, "goals/")
    stored = versions.pop(f"goals/{version}.json")
    assert stored["text"] == "Book travel and file expenses."
    assert dt.datetime.fromisoformat(stored["created_at"]).tzinfo is not None
    [(name, activation)] = versions.items()
    assert name.startswith("goals/activations/") and name.endswith(
        f"-{version}.json"
    )
    assert activation["version"] == version
    assert gcs.objects["goal.md"].decode() == "Book travel and file expenses."


def test_the_same_text_is_the_same_version(gcs, store) -> None:
    """Saving a text again reuses its version, keeping the first save time;
    every save still adds an activation."""
    goal_doc.save_goal(store, "First goal.")
    first = _goal_objects(
        gcs, f"goals/{goal_doc.compute_goal_version('First goal.')}.json"
    )
    goal_doc.save_goal(store, "Second goal.")
    goal_doc.save_goal(store, "First goal.")

    versions = [
        n
        for n in gcs.objects
        if n.startswith("goals/") and "/activations/" not in n
    ]
    activations = _goal_objects(gcs, "goals/activations/")
    assert len(versions) == 2
    assert len(activations) == 3
    assert (
        _goal_objects(
            gcs, f"goals/{goal_doc.compute_goal_version('First goal.')}.json"
        )
        == first
    )
    assert goal_doc.read_goal(store) == "First goal."


def test_a_version_id_is_the_hash_of_the_text_alone() -> None:
    assert goal_doc.compute_goal_version(
        "  Keep it short.\n"
    ) == goal_doc.compute_goal_version("Keep it short.")
    assert (
        goal_doc.compute_goal_version("Keep it short.")
        == (hashlib.sha256(b"Keep it short.").hexdigest()[:12])
    )
    assert goal_doc.compute_goal_version(
        "Keep it short."
    ) != goal_doc.compute_goal_version("Keep it long.")


def test_a_goal_over_8_kib_is_refused(gcs, store) -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        goal_doc.save_goal(store, "x" * (goal_doc.GOAL_MAX_BYTES + 1))
    assert gcs.objects == {}

    goal_doc.save_goal(store, "é" * (goal_doc.GOAL_MAX_BYTES // 2))
    assert goal_doc.read_goal(store) == "é" * (goal_doc.GOAL_MAX_BYTES // 2)


def test_no_bucket_is_an_error_not_an_empty_goal(gcs) -> None:
    """A service that cannot look must not answer "nothing is set"."""
    unconfigured = GcsObjectStore("", client_factory=lambda: gcs)
    with pytest.raises(StorageNotConfiguredError):
        goal_doc.read_goal(unconfigured)
    with pytest.raises(StorageNotConfiguredError):
        goal_doc.save_goal(unconfigured, "x")


def test_an_investigation_reads_the_saved_goal(gcs, store) -> None:
    goal_doc.save_goal(store, "Refunds first.")
    before = dict(gcs.objects)

    loaded = goal_doc.load_investigation_goal(store)

    assert loaded == goal_doc.InvestigationGoal(
        text="Refunds first.", source="saved"
    )
    assert loaded.version == goal_doc.compute_goal_version("Refunds first.")
    assert gcs.objects == before


def test_a_goal_saved_before_versions_becomes_one_when_read(gcs, store) -> None:
    """So the version an investigation records can always be looked up."""
    gcs.objects["goal.md"] = b"Refunds first."

    loaded = goal_doc.load_investigation_goal(store)

    stored = _goal_objects(gcs, f"goals/{loaded.version}.json")
    assert stored[f"goals/{loaded.version}.json"]["text"] == "Refunds first."
    assert _goal_objects(gcs, "goals/activations/") == {}


def test_no_saved_goal_is_unset(gcs, store) -> None:
    assert (
        goal_doc.load_investigation_goal(store) == goal_doc.InvestigationGoal()
    )
    assert goal_doc.InvestigationGoal().source == "unset"
    assert goal_doc.InvestigationGoal().version is None
    assert gcs.objects == {}


class _ReadFails(FakeClient):
    def bucket(self, name: str) -> Any:
        raise RuntimeError("403 on the bucket")


@pytest.mark.parametrize(
    "bucket", ["", BUCKET], ids=["no_bucket", "read_error"]
)
def test_a_goal_that_cannot_be_read_gives_no_goal_not_an_error(
    bucket: str,
) -> None:
    """Not "unset" either: the developer may have written a goal, and the run's
    record has to say it ran without one it could not read."""
    loaded = goal_doc.load_investigation_goal(
        GcsObjectStore(bucket, client_factory=_ReadFails)
    )

    assert loaded == goal_doc.InvestigationGoal(source="unreadable")


def test_an_oversized_goal_md_is_not_used(gcs, store) -> None:
    """The cap is enforced on save; a goal.md written around it is refused here
    rather than sent, whole, with every review."""
    gcs.objects["goal.md"] = b"x" * (goal_doc.GOAL_MAX_BYTES + 1)

    assert goal_doc.load_investigation_goal(store).source == "unreadable"
    assert _goal_objects(gcs, "goals/") == {}


def test_failing_to_store_the_version_still_uses_the_goal(
    gcs, monkeypatch, store
) -> None:
    gcs.objects["goal.md"] = b"Refunds first."

    def refuse(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("403 on create")

    monkeypatch.setattr(store, "write_text", refuse)

    assert goal_doc.load_investigation_goal(
        store
    ) == goal_doc.InvestigationGoal(text="Refunds first.", source="saved")


def _put_version(gcs: FakeClient, text: str, created_at: str) -> str:
    version = goal_doc.compute_goal_version(text)
    gcs.objects[f"goals/{version}.json"] = json.dumps(
        {"text": text, "created_at": created_at}
    ).encode()
    return version


def _put_activation(gcs: FakeClient, text: str, stamp: str) -> None:
    version = goal_doc.compute_goal_version(text)
    gcs.objects[f"goals/activations/{stamp}-{version}.json"] = json.dumps(
        {"version": version}
    ).encode()


def test_versions_are_listed_most_recently_active_first(gcs, store) -> None:
    first = _put_version(gcs, "First goal.", "2026-09-01T09:00:00+00:00")
    second = _put_version(gcs, "Second goal.", "2026-09-02T09:00:00+00:00")
    _put_activation(gcs, "First goal.", "20260901T090000000000Z")
    _put_activation(gcs, "Second goal.", "20260902T090000000000Z")
    _put_activation(gcs, "First goal.", "20260903T101500123456Z")

    listed = goal_doc.list_versions(store)

    assert listed == [
        goal_doc.GoalVersion(
            version=first,
            text="First goal.",
            created_at="2026-09-01T09:00:00+00:00",
            last_activated_at="2026-09-03T10:15:00.123456+00:00",
        ),
        goal_doc.GoalVersion(
            version=second,
            text="Second goal.",
            created_at="2026-09-02T09:00:00+00:00",
            last_activated_at="2026-09-02T09:00:00+00:00",
        ),
    ]


def test_saving_again_moves_a_version_to_the_top_without_a_copy(store) -> None:
    """Restoring is saving the old text again: the version it had comes back to
    the top, keeping the time it was first saved."""
    goal_doc.save_goal(store, "First goal.")
    goal_doc.save_goal(store, "Second goal.")
    created = goal_doc.list_versions(store)[1].created_at

    goal_doc.save_goal(store, "First goal.")

    listed = goal_doc.list_versions(store)
    assert [v.text for v in listed] == ["First goal.", "Second goal."]
    assert listed[0].created_at == created


def test_a_version_never_activated_is_listed_last(gcs, store) -> None:
    """A goal saved before versions existed becomes a version when an
    investigation reads it, with no activation of its own."""
    _put_version(gcs, "Active goal.", "2026-09-02T09:00:00+00:00")
    _put_activation(gcs, "Active goal.", "20260902T090000000000Z")
    _put_version(gcs, "Imported goal.", "2026-09-05T09:00:00+00:00")

    listed = goal_doc.list_versions(store)

    assert [v.text for v in listed] == ["Active goal.", "Imported goal."]
    assert listed[1].last_activated_at == ""


def test_a_version_that_does_not_parse_costs_only_itself(
    gcs, caplog, store
) -> None:
    _put_version(gcs, "Good goal.", "2026-09-02T09:00:00+00:00")
    gcs.objects["goals/0123456789ab.json"] = b"not json"

    assert [v.text for v in goal_doc.list_versions(store)] == ["Good goal."]
    assert "0123456789ab" in caplog.text


def test_only_the_most_recently_active_versions_are_read(
    gcs, monkeypatch, store
) -> None:
    monkeypatch.setattr(goal_doc, "MAX_LISTED_VERSIONS", 2)
    for day, text in enumerate(["One.", "Two.", "Three."], start=1):
        _put_version(gcs, text, f"2026-09-0{day}T09:00:00+00:00")
        _put_activation(gcs, text, f"2026090{day}T090000000000Z")

    assert [v.text for v in goal_doc.list_versions(store)] == ["Three.", "Two."]


# --------------------------------------------------------------------------- #
# The memories                                                                #
# --------------------------------------------------------------------------- #

_TEXT = "The agent's system prompt is in app/prompts/system.md."


def _stored(gcs: FakeClient, memory_id: str) -> dict:
    return json.loads(gcs.objects[f"memories/{memory_id}.json"])


def test_recording_stores_a_memory_under_an_id_from_its_text(
    gcs, store
) -> None:
    memory, created = memories_doc.record_memory(
        store, f"  {_TEXT}\n", source="chat-1"
    )

    assert created is True
    assert memory.id == hashlib.sha256(_TEXT.encode()).hexdigest()[:12]
    stored = _stored(gcs, memory.id)
    assert stored["text"] == _TEXT
    assert stored["source"] == "chat-1"
    assert dt.datetime.fromisoformat(stored["created_at"]).tzinfo is not None
    assert memories_doc.load_memories(store) == [memory]


def test_recording_the_same_text_again_changes_nothing(gcs, store) -> None:
    """The id is the text alone, so the same text from another chat is the
    same memory, and the first one is kept as it was."""
    first, _ = memories_doc.record_memory(store, _TEXT, source="chat-1")
    before = dict(gcs.objects)

    again, created = memories_doc.record_memory(store, _TEXT, source="chat-2")

    assert created is False
    assert again == first
    assert gcs.objects == before


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_a_memory_needs_text_and_at_most_500_characters(
    gcs, store, text
) -> None:
    with pytest.raises(ValueError):
        memories_doc.record_memory(store, text, source="chat-1")
    assert gcs.objects == {}

    memories_doc.record_memory(store, "é" * 500, source="chat-1")


@pytest.mark.parametrize(
    "bad_id", ["../goal", "goal", "", "ABCDEF123456", "abc/def"]
)
def test_an_id_that_is_not_a_memory_id_touches_nothing(
    gcs, store, bad_id
) -> None:
    """An id arrives from a route, so it must name a memory object and
    nothing else."""
    store.write_text("goal.md", "The goal.")

    assert memories_doc.delete_memory(store, bad_id) is False
    assert gcs.objects["goal.md"] == b"The goal."


def test_memories_are_listed_newest_first(store, monkeypatch) -> None:
    times = iter(["2026-09-01T09:00:00+00:00", "2026-09-01T12:00:00+00:00"])
    monkeypatch.setattr(memories_doc, "_now_iso", lambda: next(times))
    memories_doc.record_memory(store, "First.", source="chat-1")
    memories_doc.record_memory(store, "Second.", source="chat-1")

    assert [x.text for x in memories_doc.load_memories(store)] == [
        "Second.",
        "First.",
    ]


def test_each_memory_is_its_own_object(gcs, store) -> None:
    """Which is what makes recording and deleting one a single write."""
    a, _ = memories_doc.record_memory(store, "A.", source="chat-1")
    b, _ = memories_doc.record_memory(store, "B.", source="chat-1")

    assert sorted(gcs.objects) == sorted(
        [f"memories/{a.id}.json", f"memories/{b.id}.json"]
    )


def test_deleting_one_leaves_the_others(store) -> None:
    a, _ = memories_doc.record_memory(store, "A.", source="chat-1")
    memories_doc.record_memory(store, "B.", source="chat-1")

    assert memories_doc.delete_memory(store, a.id) is True

    assert [x.text for x in memories_doc.load_memories(store)] == ["B."]


def test_deleting_an_unknown_id_reports_it(store) -> None:
    memories_doc.record_memory(store, "A.", source="chat-1")
    assert memories_doc.delete_memory(store, "0000000000ff") is False
    assert len(memories_doc.load_memories(store)) == 1


def test_an_unreadable_memory_costs_only_itself(gcs, store) -> None:
    memories_doc.record_memory(store, "A.", source="chat-1")
    gcs.objects["memories/0000000000ff.json"] = b"{not json"

    assert len(memories_doc.load_memories(store)) == 1


def test_a_non_json_object_under_the_prefix_is_ignored(gcs, store) -> None:
    memories_doc.record_memory(store, "A.", source="chat-1")
    gcs.objects["memories/README.txt"] = b"notes"

    assert len(memories_doc.load_memories(store)) == 1


@pytest.mark.parametrize("text", [None, 7, "", "   ", ["a"]])
def test_an_object_without_text_is_skipped(gcs, store, text) -> None:
    """Rather than read back as the memory "None" or "7"."""
    gcs.objects["memories/0000000000ff.json"] = json.dumps(
        {"text": text}
    ).encode()

    assert memories_doc.load_memories(store) == []


def test_an_unreadable_object_under_the_same_id_is_replaced(gcs, store) -> None:
    """Otherwise that text could never be remembered, and the card, which
    skips the object, could not delete it."""
    memory_id = memories_doc.compute_memory_id(_TEXT)
    gcs.objects[f"memories/{memory_id}.json"] = b"{not json"

    memory, created = memories_doc.record_memory(store, _TEXT, source="chat-1")

    assert created is True
    assert _stored(gcs, memory_id)["text"] == _TEXT
    assert memories_doc.load_memories(store) == [memory]


class _VanishingStore:
    """A store whose object is deleted between each create and the read after
    it, `deletes_left` times."""

    def __init__(self, deletes_left: int) -> None:
        self.deletes_left = deletes_left
        self.objects: dict[str, str] = {}

    def list_names(self, prefix: str) -> list[str]:
        return []

    def write_text(
        self, name: str, text: str, *, overwrite: bool = True
    ) -> bool:
        if self.deletes_left:
            # Someone else created it; it is deleted before it can be read.
            self.deletes_left -= 1
            return False
        self.objects[name] = text
        return True

    def read_text(self, name: str) -> str | None:
        return self.objects.get(name)


def test_a_memory_deleted_while_being_stored_is_stored_again() -> None:
    vanishing = _VanishingStore(deletes_left=1)

    memory, created = memories_doc.record_memory(
        vanishing, _TEXT, source="chat-1"
    )

    assert created is True
    assert (
        json.loads(vanishing.objects[f"memories/{memory.id}.json"])["text"]
        == _TEXT
    )


def test_recording_gives_up_after_a_second_deletion() -> None:
    """Bounded: a store that keeps losing the object fails the call instead
    of recursing."""
    with pytest.raises(RuntimeError):
        memories_doc.record_memory(
            _VanishingStore(deletes_left=5), _TEXT, source="chat-1"
        )


def test_at_most_100_memories_are_kept(gcs, store, monkeypatch) -> None:
    monkeypatch.setattr(memories_doc, "MAX_MEMORIES", 3)
    for text in ("A.", "B.", "C."):
        memories_doc.record_memory(store, text, source="chat-1")

    with pytest.raises(ValueError, match="at most 3 memories"):
        memories_doc.record_memory(store, "D.", source="chat-1")
    again, created = memories_doc.record_memory(store, "A.", source="chat-2")

    assert created is False
    assert again.text == "A."
    assert len(memories_doc.load_memories(store)) == 3


def test_the_cap_is_100() -> None:
    assert memories_doc.MAX_MEMORIES == 100
