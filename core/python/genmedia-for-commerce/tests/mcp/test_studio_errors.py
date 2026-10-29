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

"""What the Studio's clients read of a failure: the MCP App's tools, the
agent's ``generate`` and its attachment notes show a message meant for the
user (``core.errors.user_message``), never a bucket or a setting."""

from __future__ import annotations

import collections
import time
from typing import Any

import mcp.types as mt
import pytest
from google.genai import types

from genmedia4commerce.core.errors import (
    BusyError,
    ConfigError,
    InputError,
    StorageError,
)
from genmedia4commerce.mcp import studio
from genmedia4commerce.studio import agent_tools as at
from genmedia4commerce.studio import headless, quota, service, store, turn
from genmedia4commerce.studio import media as sm

#: Details a client must never read.
_SECRET = "secret-bucket"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _guards(storage, monkeypatch: pytest.MonkeyPatch):
    """Studios in the fake bucket, no starts yet, no limits from the
    environment."""
    monkeypatch.setattr(store, "_BACKOFF_S", 0)
    monkeypatch.setattr(quota, "_STARTS", collections.deque())
    monkeypatch.delenv("GENMEDIA_MAX_JOBS_PER_HOUR", raising=False)
    monkeypatch.delenv("GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR", raising=False)
    return storage


def _error(res: mt.CallToolResult) -> str:
    """The error of a tool result, checking the model's text hides nothing
    the app's ``structuredContent`` does not."""
    assert res.isError and set(res.structuredContent) == {"error"}
    assert _SECRET not in res.content[0].text
    return res.structuredContent["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        StorageError(f"could not read gs://{_SECRET}/sessions/s1/state.json"),
        ConfigError(f"genmedia@{_SECRET}.iam.gserviceaccount.com cannot sign"),
        RuntimeError(f"/srv/{_SECRET}/key.json"),
    ],
)
async def test_server_errors_reach_the_app_as_a_generic_message(
    monkeypatch, caplog, error
) -> None:
    def fail(*_args: Any) -> dict:
        raise error

    monkeypatch.setattr(service, "state", fail)
    res = await studio.studio_state("s1")
    assert _error(res) == "studio_state failed; try again."
    assert _SECRET in caplog.text  # the details are in the log only


def _studio(**state: Any) -> None:
    """Studio s1 as the app saved it."""
    doc = {"state": {"capability": "spin", **state}, "updated_at": 1.0}
    store.write(store.state_path("s1"), doc)


def _running_job() -> None:
    """Studio s1 follows a job that is still running."""
    now = time.time()
    doc = {"status": "running", "started": now - 60, "updated_at": now - 5}
    store.write(store.job_path("old"), doc)
    _studio(job={"job_id": "old", "capability": "spin"}, job_starts=[now])


def _rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Studio s1 has started the one job it may start an hour."""
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_HOUR", "1")
    _studio(job_starts=[time.time() - 10])


def _instance_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """This instance has started the one job it may start an hour."""
    monkeypatch.setenv("GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR", "1")
    quota._STARTS.append(time.time())
    _studio()


def _not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Studio s1 cannot be saved."""
    _studio()
    monkeypatch.setattr(store, "update", lambda path, change: None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "shown"),
    [
        (lambda mp: _running_job(), quota.RUNNING),
        (_rate_limited, "1 generations in the last hour"),
        (_instance_busy, "The server is busy"),
        (_not_saved, quota.NOT_STARTED),
    ],
)
async def test_the_cost_guards_messages_reach_the_app(
    monkeypatch, setup, shown
) -> None:
    setup(monkeypatch)
    res = await studio.studio_job_start(
        {"studio_id": "s1", "capability": "spin"}
    )
    assert shown in _error(res)
    assert not quota._STARTS or setup is _instance_busy  # a refusal: no slot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (StorageError(f"could not read gs://{_SECRET}/x"), quota.NOT_STARTED),
        (ConfigError(f"{_SECRET} is not set"), quota.NOT_STARTED),
        (BusyError(quota.NOT_STARTED), quota.NOT_STARTED),
        (InputError(quota.RUNNING), quota.RUNNING),
    ],
)
async def test_generate_shows_the_model_only_messages_meant_for_the_user(
    monkeypatch, caplog, error, shown
) -> None:
    async def refused(spec: dict[str, Any]) -> dict:
        del spec
        raise error

    monkeypatch.setattr(quota, "start", refused)
    person = {"label": "me", "ref": "gs://b/me.png"}
    products = [{"label": "tee", "ref": "gs://b/tee.png"}]
    st = {"capability": "tryon", "person": person, "products": products}
    token = turn.start(turn.Scope(studio_id="s1", st=st, results={}))
    try:
        assert await at.generate() == {"error": shown}
    finally:
        turn.end(token)
    assert str(error) in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "note"),
    [
        (
            StorageError(f"gs://{_SECRET}/up.png: 403"),
            "which could not be saved.",
        ),
        (InputError("not an image"), "which cannot be used: not an image"),
    ],
)
async def test_an_attachment_note_names_no_bucket(
    monkeypatch, caplog, error, note
) -> None:
    def fail(*_args: Any) -> dict:
        raise error

    monkeypatch.setattr(sm, "save_upload", fail)
    blob = types.Blob(data=b"x", mime_type="image/png", display_name="a.png")
    msg = types.Content(role="user", parts=[types.Part(inline_data=blob)])
    out = await headless.without_attachments("s1", msg)
    assert out.parts[0].text.endswith(f"The user attached a.png, {note}")
    if isinstance(error, StorageError):
        assert _SECRET in caplog.text
