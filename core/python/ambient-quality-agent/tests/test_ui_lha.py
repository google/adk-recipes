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

"""The `/lha/*` surfaces, and the conversation list behind `/lha/contexts`.

Two things are being pinned here. That the surfaces AQuA does not have say so
rather than answering empty -- an empty list renders as "nothing here", which
is indistinguishable from a real empty state. And that the UI's own task reader
agrees with the agent's writer, since it is a deliberate second implementation
rather than an import and nothing else would catch the two drifting.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import task_reader
from google.api_core.exceptions import NotFound

# The UI is not a package -- it is flattened to the image root -- so `app` and
# `task_reader` are top-level modules. `pythonpath` in pyproject.toml puts both
# roots in place.
import app as ui_app


class _Blob:
    def __init__(
        self,
        store: dict[str, Any],
        name: str,
        undeletable: frozenset[str] = frozenset(),
    ) -> None:
        self._store = store
        self._undeletable = undeletable
        self.name = name

    def download_as_bytes(self) -> bytes:
        payload = self._store[self.name]
        if payload is _UNREADABLE:
            raise RuntimeError("503 backend error")
        return str(payload).encode("utf-8")

    def upload_from_string(self, payload: str, content_type: str = "") -> None:
        del content_type
        self._store[self.name] = payload

    def delete(self) -> None:
        if self.name in self._undeletable:
            raise RuntimeError("403 forbidden")
        if self.name not in self._store:
            # Matches Cloud Storage, which refuses to delete a missing object.
            raise NotFound(self.name)
        del self._store[self.name]


_UNREADABLE = object()


class _Client:
    """In-memory mock of `storage.Client` methods used by `task_reader`.

    Args:
        store: Mapping of object names to payloads.
        undeletable: Object names whose deletion raises an error to simulate
            partial-erase failures.
    """

    def __init__(
        self, store: dict[str, Any], undeletable: frozenset[str] = frozenset()
    ) -> None:
        self._store = store
        self._undeletable = undeletable

    def list_blobs(self, _bucket: str, prefix: str = "") -> list[_Blob]:
        return [
            _Blob(self._store, name, self._undeletable)
            for name in sorted(self._store)
            if name.startswith(prefix)
        ]

    def bucket(self, _name: str) -> Any:
        store, undeletable = self._store, self._undeletable

        class _Bucket:
            def blob(self, name: str) -> _Blob:
                return _Blob(store, name, undeletable)

        return _Bucket()


@pytest.fixture
def objects(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An in-memory `tasks/` prefix, with the bucket configured."""
    store: dict[str, Any] = {}
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "jobs")
    monkeypatch.setattr(
        task_reader, "_create_storage_client", lambda: _Client(store)
    )
    return store


def _write(
    store: dict[str, Any], task_id: str, context_id: str, stamp: str = ""
) -> None:
    """Encodes a task in proto JSON format with lowerCamelCase fields.

    Args:
        store: Storage mapping where the task object will be written.
        task_id: Unique task identifier.
        context_id: Context identifier grouping conversation tasks.
        stamp: Optional completion timestamp string in RFC 3339 format.
    """
    task: dict[str, Any] = {"id": task_id, "contextId": context_id}
    if stamp:
        task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": stamp}
    store[f"tasks/{task_id}.json"] = json.dumps(task)


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.get(path)


async def _delete(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.delete(path)


async def _put(path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.put(path, **kwargs)


def _write_with_message(
    store: dict[str, Any],
    task_id: str,
    context_id: str,
    stamp: str,
    history: list[dict[str, Any]],
) -> None:
    """Encodes a task with a history in proto JSON format.

    Args:
        store: Storage mapping where the task object will be written.
        task_id: Unique task identifier.
        context_id: Context identifier grouping conversation tasks.
        stamp: Completion timestamp string in RFC 3339 format.
        history: Messages in proto JSON form, oldest first.
    """
    _write(store, task_id, context_id, stamp)
    task = json.loads(store[f"tasks/{task_id}.json"])
    task["history"] = history
    store[f"tasks/{task_id}.json"] = json.dumps(task)


def _build_user_message(*texts: str) -> dict[str, Any]:
    """Builds a user message in proto JSON form.

    Args:
        *texts: Text of each part, in order.

    Returns:
        The message dictionary.
    """
    return {"role": "ROLE_USER", "parts": [{"text": t} for t in texts]}


def _build_agent_message(text: str) -> dict[str, Any]:
    """Builds an agent message in proto JSON form.

    Args:
        text: Text of the message's one part.

    Returns:
        The message dictionary.
    """
    return {"role": "ROLE_AGENT", "parts": [{"text": text}]}


# --- the surfaces AQuA does not have --------------------------------------- #


def test_absent_surfaces_say_so_rather_than_answering_empty() -> None:
    """An empty object renders as "nothing here", which is a lie about a
    surface that does not exist. The client also short-circuits on 501 and
    would otherwise retry a route that is never going to appear."""
    for path in (
        "/lha/state",
        "/lha/workspace/tree",
        "/lha/uploads",
        "/feedback",
    ):
        resp = asyncio.run(_get(path))

        assert resp.status_code == 501, path
        body = resp.json()
        assert body["error"] == "not available in AQuA"
        assert body["detail"], f"{path} gives no reason"


def test_both_route_shapes_are_answered() -> None:
    """`/lha/uploads` and `/lha/uploads/x` are separate paths to FastAPI, so
    wiring one leaves the other unanswered."""
    for path in ("/lha/uploads", "/lha/uploads/nested/deeper"):
        assert asyncio.run(_get(path)).status_code == 501, path


def test_sessions_answers_empty_because_that_list_is_client_side() -> None:
    """Not an error: the list genuinely lives in the browser, and 501 here
    would make the sidebar look broken rather than empty."""
    resp = asyncio.run(_get("/lha/sessions"))

    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


# --- the conversation list -------------------------------------------------- #


def test_contexts_groups_tasks_into_conversations(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-a", "2026-09-02T10:00:00Z")
    _write(objects, "t3", "ctx-b", "2026-09-03T10:00:00Z")

    resp = asyncio.run(_get("/lha/contexts"))

    assert resp.status_code == 200
    contexts = resp.json()["contexts"]
    # Newest first: the sidebar renders this order, so it is the contract.
    assert [c["context_id"] for c in contexts] == ["ctx-b", "ctx-a"]
    assert sorted(contexts[1]["task_ids"]) == ["t1", "t2"]
    # A conversation is as recent as its newest task.
    assert contexts[1]["updated_at"] == "2026-09-02T10:00:00Z"


def test_contexts_list_a_conversations_tasks_oldest_first(
    objects: dict[str, Any],
) -> None:
    """The chat replays a conversation in this order. Task ids carry no time,
    so the listing's name order says nothing about when a turn ran."""
    _write(objects, "t-a", "ctx-a", "2026-09-02T10:00:00Z")
    _write(objects, "t-b", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t-c", "ctx-a", "2026-09-03T10:00:00Z")

    resp = asyncio.run(_get("/lha/contexts"))

    assert resp.json()["contexts"][0]["task_ids"] == ["t-b", "t-a", "t-c"]


def test_both_contexts_paths_answer(objects: dict[str, Any]) -> None:
    """The bare path would otherwise fall through to the 501 catch-all,
    leaving the answer to depend on how the client spells the URL."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")

    for path in ("/lha/contexts", "/lha/contexts/"):
        resp = asyncio.run(_get(path))

        assert resp.status_code == 200, path
        assert resp.json()["contexts"][0]["context_id"] == "ctx-a", path


def test_an_unconfigured_bucket_says_so_rather_than_showing_no_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that cannot look and a user with no chats are different
    facts, and only one of them is the user's problem."""
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "")

    resp = asyncio.run(_get("/lha/contexts"))

    assert resp.status_code == 501
    assert resp.json()["detail"]


def test_a_failed_read_is_an_error_not_an_empty_sidebar(
    objects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_bucket: str) -> Any:
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(task_reader, "_load_all", boom)

    resp = asyncio.run(_get("/lha/contexts"))

    assert resp.status_code == 502
    assert "403" in resp.json()["error"]
    assert resp.json()["contexts"] == []


def test_one_unreadable_task_does_not_empty_the_listing(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-b", "2026-09-02T10:00:00Z")
    objects["tasks/t1.json"] = "not json"

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert [c["context_id"] for c in contexts] == ["ctx-b"]


def test_a_task_with_no_status_is_still_listed(objects: dict[str, Any]) -> None:
    """Proto JSON omits unset fields, so a task that never carried a status
    has no `status` key at all -- it must not crash the listing."""
    _write(objects, "t1", "ctx-a")

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert contexts[0]["context_id"] == "ctx-a"
    assert contexts[0]["updated_at"] == ""


# --- one conversation's tasks ----------------------------------------------- #


def test_the_tasks_route_returns_tasks_not_contexts(
    objects: dict[str, Any],
) -> None:
    """Theirs returned the contexts list from this path, so the caller that
    prefetches chat history has always been dead behind a `catch`."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-a", "2026-09-02T10:00:00Z")
    _write(objects, "t3", "ctx-b", "2026-09-03T10:00:00Z")

    resp = asyncio.run(_get("/lha/contexts/ctx-a/tasks"))

    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    # Oldest first: a transcript replays forwards.
    assert [t["id"] for t in tasks] == ["t1", "t2"]


def test_an_unknown_conversation_has_no_tasks(objects: dict[str, Any]) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")

    resp = asyncio.run(_get("/lha/contexts/nope/tasks"))

    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


# --- deleting a conversation -------------------------------------------------- #


def test_deleting_a_conversation_removes_its_tasks_and_no_others(
    objects: dict[str, Any],
) -> None:
    """Verifies that deleting a conversation removes all associated task blobs from storage."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-a", "2026-09-02T10:00:00Z")
    _write(objects, "t3", "ctx-b", "2026-09-03T10:00:00Z")

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    assert sorted(objects) == ["tasks/t3.json"]
    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]
    assert [c["context_id"] for c in contexts] == ["ctx-b"]


def test_a_delete_never_touches_objects_outside_the_tasks_prefix(
    objects: dict[str, Any],
) -> None:
    """Verifies deletions are restricted to the `tasks/` prefix to protect run state and telemetry."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    objects["aqa-jobs/run-1.json"] = json.dumps({"contextId": "ctx-a"})
    objects["aqa-telemetry/2026-09-01/part-0.json"] = json.dumps(
        {"contextId": "ctx-a"}
    )

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.json() == {"deleted": 1}
    assert sorted(objects) == [
        "aqa-jobs/run-1.json",
        "aqa-telemetry/2026-09-01/part-0.json",
    ]


def test_an_empty_context_id_does_not_erase_every_malformed_task(
    objects: dict[str, Any],
) -> None:
    """A task stored without a `contextId` reads as `""`, so an empty id must not
    match it -- one such request would otherwise wipe every malformed blob."""
    objects["tasks/orphan.json"] = json.dumps({"id": "orphan"})
    _write(objects, "t1", "", "2026-09-01T10:00:00Z")

    assert asyncio.run(task_reader.delete_context("jobs", "")) == 0
    assert sorted(objects) == ["tasks/orphan.json", "tasks/t1.json"]


def test_deleting_an_unknown_conversation_is_not_an_error(
    objects: dict[str, Any],
) -> None:
    """Verifies deleting a nonexistent conversation succeeds idempotently with zero deletions."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")

    resp = asyncio.run(_delete("/lha/contexts/nope"))

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}
    assert sorted(objects) == ["tasks/t1.json"]


def test_a_delete_without_a_bucket_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "")

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.status_code == 501
    assert resp.json()["error"] == "not available in AQuA"
    assert resp.json()["detail"]


def test_a_bucket_that_cannot_be_listed_is_an_error(
    objects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Any:
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(task_reader, "_create_storage_client", boom)

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.status_code == 502
    assert "403" in resp.json()["error"]


def test_a_partial_erase_is_never_reported_as_a_clean_one(
    objects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies partial deletion failures return an error rather than reporting success."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-a", "2026-09-02T10:00:00Z")
    monkeypatch.setattr(
        task_reader,
        "_create_storage_client",
        lambda: _Client(objects, frozenset({"tasks/t2.json"})),
    )

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.status_code == 502
    assert "403" in resp.json()["error"]
    assert "deleted" not in resp.json()
    assert sorted(objects) == ["tasks/t2.json"]


# --- the first message --------------------------------------------------------- #


def test_contexts_report_the_first_user_message_of_the_oldest_task(
    objects: dict[str, Any],
) -> None:
    """A browser that never saw the chat builds its name from this field, so it
    must hold the opening message rather than a later turn."""
    _write_with_message(
        objects,
        "t-new",
        "ctx-a",
        "2026-09-02T10:00:00Z",
        [_build_user_message("a later question")],
    )
    _write_with_message(
        objects,
        "t-old",
        "ctx-a",
        "2026-09-01T10:00:00Z",
        [
            _build_agent_message("hello"),
            _build_user_message("why did", "  create_ticket\n fail?"),
            _build_user_message("x"),
        ],
    )

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert contexts[0]["first_message"] == "why did create_ticket fail?"


def test_a_conversation_with_no_user_text_has_no_first_message(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write_with_message(
        objects,
        "t2",
        "ctx-b",
        "2026-09-02T10:00:00Z",
        [
            {"role": "ROLE_USER", "parts": [{"url": "gs://x"}]},
            _build_agent_message("hi"),
        ],
    )

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert [c["first_message"] for c in contexts] == [None, None]


def test_contexts_report_the_agent_the_first_user_message_names(
    objects: dict[str, Any],
) -> None:
    """The dashboard lists each agent's conversations apart, by this field."""
    _write_with_message(
        objects,
        "t1",
        "ctx-a",
        "2026-09-01T10:00:00Z",
        [
            _build_user_message("no key on this one"),
            {
                **_build_user_message("why?"),
                "metadata": {"agent_key": "acme-prod__support"},
            },
        ],
    )

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert contexts[0]["agent_key"] == "acme-prod__support"


def test_a_conversation_whose_messages_name_no_agent_has_no_agent_key(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write_with_message(
        objects,
        "t2",
        "ctx-b",
        "2026-09-02T10:00:00Z",
        [
            {**_build_user_message("hi"), "metadata": {"agent_key": "  "}},
            {**_build_agent_message("hi"), "metadata": {"agent_key": "x"}},
        ],
    )

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert [c["agent_key"] for c in contexts] == [None, None]


def test_the_first_message_is_bounded(objects: dict[str, Any]) -> None:
    _write_with_message(
        objects,
        "t1",
        "ctx-a",
        "2026-09-01T10:00:00Z",
        [_build_user_message("y" * 5000)],
    )

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert contexts[0]["first_message"] == (
        "y" * task_reader.FIRST_MESSAGE_MAX_LENGTH
    )


# --- chat names ------------------------------------------------------------------ #


def test_a_saved_name_is_listed_with_its_conversation(
    objects: dict[str, Any],
) -> None:
    """The server must return the saved name so other browsers do not fall back
    to the first message or "Untitled chat"."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-b", "2026-09-02T10:00:00Z")

    resp = asyncio.run(
        _put("/lha/contexts/ctx-a/title", json={"title": "  Ticket bug  "})
    )

    assert resp.status_code == 200
    assert resp.json() == {"title": "Ticket bug"}
    assert json.loads(objects["chat-titles/ctx-a.json"]) == {
        "title": "Ticket bug"
    }
    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]
    assert {c["context_id"]: c["title"] for c in contexts} == {
        "ctx-a": "Ticket bug",
        "ctx-b": None,
    }


def test_a_second_rename_replaces_the_first(objects: dict[str, Any]) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")

    asyncio.run(_put("/lha/contexts/ctx-a/title", json={"title": "one"}))
    asyncio.run(_put("/lha/contexts/ctx-a/title", json={"title": "two"}))

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]
    assert contexts[0]["title"] == "two"


def test_a_name_for_an_unknown_conversation_is_not_listed(
    objects: dict[str, Any],
) -> None:
    """The listing includes only conversations that have tasks; a saved name
    alone does not create a conversation entry."""
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    objects["chat-titles/ctx-gone.json"] = json.dumps({"title": "orphan"})

    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert [(c["context_id"], c["title"]) for c in contexts] == [
        ("ctx-a", None)
    ]


def test_one_unreadable_name_does_not_fail_the_listing(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    _write(objects, "t2", "ctx-b", "2026-09-02T10:00:00Z")
    objects["chat-titles/ctx-a.json"] = _UNREADABLE
    objects["chat-titles/ctx-b.json"] = json.dumps({"title": "kept"})

    resp = asyncio.run(_get("/lha/contexts"))

    assert resp.status_code == 200
    assert {c["context_id"]: c["title"] for c in resp.json()["contexts"]} == {
        "ctx-a": None,
        "ctx-b": "kept",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"title": ""},
        {"title": "   "},
        {"title": None},
        {"title": 7},
        {},
        ["title"],
        {"title": "x" * 201},
    ],
)
def test_a_blank_or_oversized_name_is_refused(
    objects: dict[str, Any], body: Any
) -> None:
    resp = asyncio.run(_put("/lha/contexts/ctx-a/title", json=body))

    assert resp.status_code == 400
    assert resp.json()["error"]
    assert not [name for name in objects if name.startswith("chat-titles/")]


def test_a_name_at_the_cap_is_saved(objects: dict[str, Any]) -> None:
    resp = asyncio.run(
        _put("/lha/contexts/ctx-a/title", json={"title": "x" * 200})
    )

    assert resp.status_code == 200


def test_a_non_json_rename_is_refused(objects: dict[str, Any]) -> None:
    resp = asyncio.run(
        _put(
            "/lha/contexts/ctx-a/title",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "Expected a JSON body."


def test_a_rename_without_a_bucket_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client falls back to a local rename on HTTP 501."""
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "")

    resp = asyncio.run(_put("/lha/contexts/ctx-a/title", json={"title": "x"}))

    assert resp.status_code == 501
    assert resp.json()["error"] == "not available in AQuA"


def test_a_failed_save_is_an_error(
    objects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_forbidden() -> Any:
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(task_reader, "_create_storage_client", raise_forbidden)

    resp = asyncio.run(_put("/lha/contexts/ctx-a/title", json={"title": "x"}))

    assert resp.status_code == 502
    assert resp.json()["error"] == "RuntimeError: 403 forbidden"


def test_the_rename_route_is_not_shadowed_by_the_lha_catch_all(
    objects: dict[str, Any],
) -> None:
    """FastAPI matches in registration order, so a rename registered after
    `/lha/{area}/{rest:path}` would answer 501 in every deployment."""
    resp = asyncio.run(_put("/lha/contexts/ctx-a/title", json={"title": "x"}))

    assert resp.status_code == 200
    assert "chat-titles/ctx-a.json" in objects


def test_a_context_id_with_a_slash_stays_under_the_titles_prefix(
    objects: dict[str, Any],
) -> None:
    """A slash would otherwise let a name overwrite a task transcript or an
    investigation artifact in the same bucket."""
    asyncio.run(task_reader.save_context_title("jobs", "../tasks/t1", "x"))

    assert sorted(objects) == ["chat-titles/.._tasks_t1.json"]


def test_an_empty_context_id_cannot_be_named(objects: dict[str, Any]) -> None:
    """Tasks without a `contextId` all read as `""`, so one name would label
    every malformed conversation."""
    with pytest.raises(ValueError):
        asyncio.run(task_reader.save_context_title("jobs", "", "x"))

    assert objects == {}


def test_deleting_a_conversation_removes_its_saved_name(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")
    objects["chat-titles/ctx-a.json"] = json.dumps({"title": "gone"})
    objects["chat-titles/ctx-b.json"] = json.dumps({"title": "kept"})

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.status_code == 200
    # The count includes only task blobs, not the saved name.
    assert resp.json() == {"deleted": 1}
    assert sorted(objects) == ["chat-titles/ctx-b.json"]


def test_deleting_a_conversation_with_no_saved_name_is_not_an_error(
    objects: dict[str, Any],
) -> None:
    _write(objects, "t1", "ctx-a", "2026-09-01T10:00:00Z")

    resp = asyncio.run(_delete("/lha/contexts/ctx-a"))

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1}
    assert objects == {}


# --- the contract with the agent's writer ------------------------------------ #


def test_the_reader_parses_what_the_agents_writer_produces(
    objects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`task_reader` is a second implementation, not an import: the UI image
    ships neither the agent package nor a2a-sdk. Nothing but this test would
    notice the two drifting, so drive the agent's real `GcsTaskStore` and read
    its bytes back through the UI's reader.

    This is what pins `contextId` as lowerCamelCase, the `tasks/<id>.json`
    layout, the RFC 3339 timestamp the sort depends on, and the `history`
    role, text and metadata encoding the first message is read from.
    """
    from a2a.types import a2a_pb2
    from ambient_quality_agent.app_utils.task_store import GcsTaskStore
    from ambient_quality_agent.tools import gcs

    written: dict[str, Any] = {}

    class _WriterBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def upload_from_string(
            self, payload: str, content_type: str = ""
        ) -> None:
            written[self.name] = payload

    class _WriterClient:
        def bucket(self, _name: str) -> Any:
            return type("B", (), {"blob": staticmethod(_WriterBlob)})()

    task = a2a_pb2.Task()
    task.id = "t-real"
    task.context_id = "ctx-real"
    task.status.state = a2a_pb2.TaskState.Value("TASK_STATE_COMPLETED")
    task.status.timestamp.FromJsonString("2026-09-07T12:00:00Z")
    question = task.history.add()
    question.role = a2a_pb2.Role.Value("ROLE_USER")
    question.parts.add().text = "why did create_ticket fail?"
    question.metadata.update({"agent_key": "acme-prod__support"})
    answer = task.history.add()
    answer.role = a2a_pb2.Role.Value("ROLE_AGENT")
    answer.parts.add().text = "It timed out."

    monkeypatch.setattr(gcs, "create_storage_client", _WriterClient)
    asyncio.run(GcsTaskStore("jobs").save(task, None))

    # The agent's bytes, read by the UI's independent reader.
    objects.update(written)
    contexts = asyncio.run(_get("/lha/contexts")).json()["contexts"]

    assert written and next(iter(written)) == "tasks/t-real.json"
    assert contexts == [
        {
            "context_id": "ctx-real",
            "task_ids": ["t-real"],
            "updated_at": "2026-09-07T12:00:00Z",
            "title": None,
            "first_message": "why did create_ticket fail?",
            "agent_key": "acme-prod__support",
        }
    ]


def _use_anonymous_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets the storage client build without real credentials.

    Args:
        monkeypatch: Replaces `google.auth.default` for the calling test.
    """
    import google.auth
    from google.auth.credentials import AnonymousCredentials

    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **k: (AnonymousCredentials(), "test-project"),
    )


def test_the_storage_client_sends_the_product_token(
    monkeypatch: pytest.MonkeyPatch, usage_analytics_on
) -> None:
    from ambient_quality_shared.usage_headers import build_product_token

    _use_anonymous_credentials(monkeypatch)

    client = task_reader._create_storage_client()

    assert client._connection.user_agent.startswith(f"{build_product_token()} ")


def test_the_storage_client_is_the_librarys_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_shared import usage_headers
    from google.cloud import storage

    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)
    _use_anonymous_credentials(monkeypatch)

    client = task_reader._create_storage_client()

    assert (
        client._connection.user_agent
        == storage.Client(project="test-project")._connection.user_agent
    )
