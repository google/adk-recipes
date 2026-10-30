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

"""The studio state, owned by the server (``state.json``).

The app sends one action per user change (``apply_action``) and renders the
state it gets back; an agent turn's patches are applied by ``save_patched``
at the end of the turn; a generation that ended is recorded by
``finish_job``. Every write re-reads the saved state and applies its change
with a generation precondition (``store.update``), so writers on different
instances never overwrite each other. Patches and actions are applied by
``studio.patches``.
"""

from __future__ import annotations

import time
from typing import Any

from genmedia4commerce.core.errors import InputError, StorageError
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import picks, store
from genmedia4commerce.studio.events import narrate_action

__all__ = [
    "apply_action",
    "finish_job",
    "get_state",
    "last_results",
    "save_patched",
    "saved_state",
    "set_last_results",
]

_KEEP_FINISHED = 5  # ended job ids kept, so a late job_started is ignored
#: Looks for a client's started job (its first save can lag job_start).
_JOB_TRIES = 6
_JOB_WAIT_S = 0.5


def _state_of(doc: dict[str, Any] | None) -> dict[str, Any]:
    """The state of a stored document (refs of older studios mapped to the
    catalogue, ``picks.upgrade_state``)."""
    st = (doc or {}).get("state") or {"capability": "tryon"}
    picks.upgrade_state(st)
    return st


def get_state(studio_id: str) -> dict[str, Any]:
    """The latest state of a studio, or an empty Photo Try-On one if it was
    never saved; raises ``StorageError`` when it cannot be read."""
    return _state_of(store.read(store.state_path(studio_id)))


def set_last_results(studio_id: str, capability: str, results: list) -> None:
    """Store the results of the studio's latest generation."""
    doc = {"capability": capability, "results": results}
    store.write(store.results_path(studio_id), doc)


def last_results(studio_id: str) -> dict[str, Any]:
    """``{capability, results}`` of the latest generation, or ``{}``;
    raises ``StorageError`` when it cannot be read."""
    return store.read(store.results_path(studio_id)) or {}


def _update(studio_id: str, change: Any) -> dict[str, Any] | None:
    """Apply ``change(state)`` to the saved state, re-read and applied again
    until no other writer got in between (``store.update``): the new
    ``{state, updated_at}``, or ``None`` when it could not be stored
    (logged). A state that cannot be read is never overwritten."""
    done: dict[str, Any] = {}

    def write(doc: dict[str, Any] | None) -> dict[str, Any]:
        st = _state_of(doc)
        change(st)
        done.update(state=st, updated_at=time.time())
        return dict(done)

    return done if store.update(store.state_path(studio_id), write) else None


def _saved_or_raise(studio_id: str, change: Any) -> dict[str, Any]:
    """``_update``, raising ``StorageError`` when it could not be stored."""
    if not (saved := _update(studio_id, change)):
        raise StorageError("The studio could not be saved; try again.")
    return saved


def save_patched(studio_id: str, patches: list[dict[str, Any]]) -> float:
    """Apply an agent turn's patches to the saved state and store it, so a
    reopened panel shows what a tool result asked the open app to show.

    Returns the ``updated_at`` it saved, or 0 when it could not be stored.
    """

    def patched(st: dict[str, Any]) -> None:
        for p in patches:
            rules.apply_patch(st, p)

    saved = _update(studio_id, patched)
    return saved["updated_at"] if saved else 0.0


# ---- actions of the app ----------------------------------------------------------


def _check_job(studio_id: str, value: Any) -> None:
    """A client's ``job_started``: ``InputError`` unless it names a job of
    this studio (``jobs.status``: this instance's, or saved by its task,
    which can take a moment after ``job_start``)."""
    from genmedia4commerce.studio import jobs  # jobs imports this module

    job_id = value.get("job_id") if isinstance(value, dict) else None
    job_id = store.check_job_id(job_id)
    job = jobs.status(job_id)
    for _ in range(_JOB_TRIES - 1):
        if job.get("studio_id") is not None:
            break
        time.sleep(_JOB_WAIT_S)
        job = jobs.status(job_id)
    if job.get("studio_id") != studio_id:
        raise InputError("Not a generation of this studio.")


def _prepare(
    studio_id: str, action: Any
) -> tuple[dict[str, Any], dict[str, str], dict]:
    """A checked copy of an app action with its catalogue picks as
    ``gs://`` copies (made in parallel, no model call); also ``{catalogue
    ref: gs:// uri}`` and ``{uri: description}`` (the server's, of the
    catalogue copies and uploads)."""
    action = rules.check_action(action)
    if "job_started" in action:
        _check_job(studio_id, action["job_started"])
    items: list[Any] = []
    for value in (action.get("set"), action.get("add")):
        if isinstance(value, dict):
            items += [value["item"]] if "item" in value else []
            if isinstance(value.get("items"), list):
                limit = rules.MAX_ITEMS[value["field"]]
                value["items"] = value["items"][:limit]
                items += value["items"]
    picked: list[str] = []
    for item in items:
        ref = item.get("ref") if isinstance(item, dict) else None
        ref = picks.legacy(ref) if isinstance(ref, str) else ref
        if not (isinstance(ref, str) and sm.allowed_ref(ref, studio_id)):
            raise InputError(f"Not an allowed image: {str(ref)[:200]}")
        picked.append(ref)
    copies = sm.catalog_copies(picked)
    refs: dict[str, str] = {}
    descriptions = sm.upload_descriptions(picked)
    for item, ref in zip(items, picked, strict=True):
        if ref in copies:
            uri, desc = copies[ref]
            refs[item["ref"]], descriptions[uri], item["ref"] = uri, desc, uri
    return action, refs, descriptions


def _removed(action: dict[str, Any], st: dict[str, Any]) -> Any:
    """The item a ``remove`` or ``clear`` action takes away, if one."""
    rm, clear = action.get("remove") or {}, action.get("clear") or {}
    if rm.get("field") in rules.LIST_FIELDS:
        items, i = (
            st.get(rules.state_key(rm["field"])) or [],
            rules.index_of(rm.get("index")),
        )
        return items[i] if 0 <= i < len(items) else None
    return (
        st.get(clear["field"])
        if clear.get("field") in rules.SLOT_FIELDS
        else None
    )


def apply_action(studio_id: str, action: Any) -> dict[str, Any]:
    """Apply a user action from the app to the saved state and note it in
    the agent's history: ``{state, updated_at, refs}``, ``refs`` mapping the
    catalogue picks to their ``gs://`` copies.

    Raises ``InputError`` for an unknown action, setting or image, and
    ``StorageError`` when the state could not be saved.
    """
    store.check_id(studio_id)
    action, refs, descriptions = _prepare(studio_id, action)
    removed: list[Any] = [None]

    def change(st: dict[str, Any]) -> None:
        removed[0] = _removed(action, st)
        rules.apply_patch(st, action)

    saved = _saved_or_raise(studio_id, change)
    if text := narrate_action(action, removed[0], descriptions):
        sh.note(studio_id, text)
    return {**saved, "refs": refs}


# ---- generations -------------------------------------------------------------------


def _ended(st: dict[str, Any], job_id: str) -> None:
    """Remember that a job ended, so a late ``job_started`` is ignored."""
    finished = [j for j in st.get("finished_jobs") or [] if j != job_id]
    st["finished_jobs"] = [job_id, *finished][:_KEEP_FINISHED]


def _end_job(st: dict[str, Any], job_id: str, job: dict[str, Any]) -> None:
    """Stop following an ended job and record its results on its page: a
    finished one's; a video try-on that animated try-on images also when
    its videos failed, with those images (``images``); a "Make video"
    (``tryon_image``), done or not, in place of that image's videos."""
    st["job"] = None
    _ended(st, job_id)
    cap = job.get("capability")
    if cap not in rules.CAPABILITIES:
        return
    results = [
        rules.clean(r, rules.RESULT_KEYS)
        for r in job.get("results") or []
        if isinstance(r, dict)
    ]
    image = job.get("tryon_image") if cap == "video_tryon" else ""
    if image and isinstance(image, str):
        rules.replace_videos(st, image, results)
        return
    images = [i for i in job.get("images") or [] if isinstance(i, dict)]
    if cap == "video_tryon" and images:
        rules.per_page(st, "images")[cap] = [
            rules.clean(i, rules.IMAGE_KEYS) for i in images
        ]
    elif job.get("status") != "done":
        return
    rules.per_page(st, "results")[cap] = results


def _drop_foreign(studio_id: str, job_id: str) -> dict[str, Any]:
    """Stop following another studio's job, its results unrecorded, so the
    studio is never stuck on it."""
    doc = store.read(store.state_path(studio_id)) or {}
    if rules.job_id_of(_state_of(doc)) != job_id:
        return {
            "state": _state_of(doc),
            "updated_at": doc.get("updated_at") or 0,
        }

    def change(st: dict[str, Any]) -> None:
        if rules.job_id_of(st) == job_id:
            st["job"] = None
            _ended(st, job_id)

    return _saved_or_raise(studio_id, change)


def finish_job(studio_id: str, job_id: str, job: dict[str, Any]) -> dict:
    """Record a generation that ended (its ``jobs.status``): a finished
    one's results go to ``results.json`` and, while the studio follows that
    job, to the state's results of its page (as ``_end_job`` says: video
    try-ons also when their videos failed); the studio then stops
    following it. A job that ended before the studio followed it is
    remembered as ended, so its late ``job_started`` is ignored. Another
    studio's job is only stopped being followed. Idempotent. Returns
    ``{state, updated_at}``."""
    if job.get("studio_id") not in (None, "", studio_id):
        return _drop_foreign(studio_id, job_id)
    if job.get("status") == "done":
        cap = str(job.get("capability"))
        set_last_results(studio_id, cap, job.get("results") or [])
    doc = store.read(store.state_path(studio_id)) or {}
    st = _state_of(doc)
    # Only a followed job is ended as "unknown": a job is unknown to
    # other instances until its first save.
    over = job.get("status") in ("done", "error")
    ended = over or job.get("status") == "unknown"

    def early(st: dict[str, Any]) -> bool:  # ended before it was followed
        return over and job_id not in (st.get("finished_jobs") or [])

    if not ((ended and rules.job_id_of(st) == job_id) or early(st)):
        return {"state": st, "updated_at": doc.get("updated_at") or 0}

    def change(st: dict[str, Any]) -> None:
        if rules.job_id_of(st) == job_id:
            _end_job(st, job_id, job)
        elif early(st):
            _ended(st, job_id)

    return _saved_or_raise(studio_id, change)


def saved_state(studio_id: str) -> dict[str, Any]:
    """The saved studio with renewed preview URLs, for the app to restore:
    ``{studio_id, saved, updated_at, state}``. Raises ``StorageError`` when
    it cannot be read, so the app never takes a failed read for a new
    studio."""
    doc = store.read(store.state_path(studio_id)) or {}
    state = doc.get("state") or {}
    picks.upgrade_state(state)
    for cap, results in (state.get("results") or {}).items():
        sm.refresh_urls(results or [], video=cap != "tryon")
    for images in (state.get("images") or {}).values():
        sm.refresh_urls(images or [], video=False)
    return {
        "studio_id": studio_id,
        "saved": bool(state),
        "updated_at": doc.get("updated_at") or 0,
        "state": state,
    }
