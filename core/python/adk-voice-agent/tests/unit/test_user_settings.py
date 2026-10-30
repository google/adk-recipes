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

"""Per-user settings, thread caps, non-blocking credential reads, and refresh-token rotation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
from google.adk.agents.run_config import RunConfig

from app.api.mcp_gateway import load_upstreams, upstream_credentials
from app.app_utils.http_client import set_default_http_transport
from app.auth.credential_store import (
    SecretManagerCredentialStore,
    set_credential_store,
)
from app.auth.identity import run_as
from app.auth.oauth import ensure_fresh_access_token
from app.store.user_settings import get_user_settings_store
from app.threads.registry import ThreadError
from app.user_settings import (
    DEFAULT_HARNESS,
    SAVE_TO_DRIVE,
    TIMEZONE,
    VOICE_NAME,
    load_user_settings,
    reset_user_settings_cache,
    save_user_settings,
    user_default_harness,
    user_settings,
)
from app.voice.instruction import build_orchestrator_instruction
from app.voice.voice_mode import VoiceSessionPlugin
from tests.fake_integrations import iap_headers, integrations_transport
from tests.fakes import (
    GOAL,
    REPO,
    FakeSecretManagerClient,
    InMemoryCredentialStore,
    Lab,
    configure_apps,
)

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest_asyncio.fixture
async def api(iap_user: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    from app.fast_api_app import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_settings_rows_are_per_user_and_none_restores_the_default():
    store = get_user_settings_store()
    await store.save(ALICE, {TIMEZONE: "UTC", SAVE_TO_DRIVE: "off"})
    await store.save(BOB, {TIMEZONE: "Asia/Tokyo"})

    assert await store.load(ALICE) == {
        TIMEZONE: "UTC",
        SAVE_TO_DRIVE: "off",
    }
    assert await store.save(
        ALICE, {TIMEZONE: "Europe/Paris", "missing": None}
    ) == {
        TIMEZONE: "Europe/Paris",
        SAVE_TO_DRIVE: "off",
    }
    assert await store.save(ALICE, {TIMEZONE: None}) == {SAVE_TO_DRIVE: "off"}
    assert await store.load(BOB) == {TIMEZONE: "Asia/Tokyo"}
    assert await store.load("carol@example.com") == {}


async def test_a_load_that_started_before_a_save_never_caches_the_old_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = get_user_settings_store()
    await store.save(ALICE, {SAVE_TO_DRIVE: "on"})
    real_load = store.load
    read, release = asyncio.Event(), asyncio.Event()

    async def slow_load(user: str) -> dict[str, Any]:
        values = await real_load(user)
        if not read.is_set():
            read.set()
            await release.wait()
        return values

    monkeypatch.setattr(store, "load", slow_load)
    with run_as(ALICE):
        stale = asyncio.create_task(load_user_settings(force=True))
        await read.wait()
        await save_user_settings({SAVE_TO_DRIVE: "off"})
        release.set()
        assert (await stale)[SAVE_TO_DRIVE] == "on"
        assert user_settings()[SAVE_TO_DRIVE] == "off"


async def test_preferences_and_harness_are_per_user(
    api: httpx.AsyncClient,
):
    alice, bob = iap_headers(ALICE), iap_headers(BOB)

    await api.patch(
        "/api/v1/preferences",
        json={
            "timezone": "UTC",
            "voice_name": "Kore",
        },
        headers=alice,
    )
    await api.post(
        "/api/v1/harnesses/default", json={"harness": "agy"}, headers=alice
    )

    def summary(config: dict[str, Any]) -> tuple[Any, ...]:
        default = [h["name"] for h in config["harnesses"] if h["is_default"]]
        prefs = config["preferences"]
        return (prefs["timezone"], prefs["voice_name"], default)

    reset_user_settings_cache()
    alice_config = (await api.get("/api/v1/config", headers=alice)).json()
    bob_config = (await api.get("/api/v1/config", headers=bob)).json()
    assert summary(alice_config) == ("UTC", "Kore", ["antigravity"])
    assert summary(bob_config) == ("America/Chicago", "Aoede", ["claude"])


async def test_the_voice_agent_uses_the_acting_users_preferences(lab: Lab):
    with run_as(ALICE):
        await save_user_settings(
            {
                TIMEZONE: "UTC",
                VOICE_NAME: "Kore",
                DEFAULT_HARNESS: "antigravity",
            }
        )
    reset_user_settings_cache()

    async def voice_and_instruction() -> tuple[str, str]:
        session = SimpleNamespace(
            session=SimpleNamespace(state={}), run_config=RunConfig()
        )
        await VoiceSessionPlugin().before_run_callback(
            invocation_context=session  # type: ignore[arg-type]
        )
        voice = (
            session.run_config.speech_config.voice_config.prebuilt_voice_config
        )
        return voice.voice_name or "", build_orchestrator_instruction(None)  # type: ignore[arg-type]

    with run_as(ALICE):
        await load_user_settings()
        voice, instruction = await voice_and_instruction()
    assert voice == "Kore"
    assert "The user's timezone is UTC" in instruction
    assert "the default is `antigravity`" in instruction

    with run_as(BOB):
        await load_user_settings()
        voice, instruction = await voice_and_instruction()
    assert voice == "Aoede"
    assert "The user's timezone is America/Chicago" in instruction
    assert "the default is `claude`" in instruction


async def test_mcp_upstreams_are_the_acting_users_connections():
    store = InMemoryCredentialStore()
    store.set_many(ALICE, {"GITHUB_ACCESS_TOKEN": "ghu-alice"})
    set_credential_store(store)

    with run_as(ALICE):
        assert await _connected_upstreams() == {"github"}
    with run_as(BOB):
        assert await _connected_upstreams() == set()


async def _connected_upstreams() -> set[str]:
    return {
        name
        for name, upstream in load_upstreams().items()
        if await upstream_credentials(upstream)
    }


async def test_threads_use_the_dispatching_users_default_harness(lab: Lab):
    with run_as(ALICE):
        await save_user_settings({DEFAULT_HARNESS: "antigravity"})
        assert user_default_harness().name == "antigravity"
        alice_thread = await lab.dispatch()
    with run_as(BOB):
        bob_thread = await lab.dispatch()
    assert (alice_thread.harness, bob_thread.harness) == (
        "antigravity",
        "claude",
    )


async def test_one_user_cannot_take_every_running_slot(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
):
    from app.fast_api_app import app

    monkeypatch.setenv("MAX_RUNNING_THREADS_PER_USER", "1")
    lab.agent.script({"steps": [{"hang": True}]})
    with run_as(ALICE):
        running = await lab.registry.dispatch(GOAL, REPO)
        await lab.wait_for_calls(1)
        with pytest.raises(
            ThreadError, match="You already have 1 thread running"
        ):
            await lab.registry.dispatch(GOAL, REPO)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        refused = await api.post(
            "/api/v1/threads",
            json={"goal": GOAL, "repo": REPO},
            headers=iap_headers(ALICE),
        )
        assert refused.status_code == 409
        assert "the most one person can run at once" in refused.json()["detail"]

    with run_as(BOB):
        bob_thread = await lab.registry.dispatch(GOAL, REPO)
        assert bob_thread.owner == BOB
        await lab.wait_for_calls(2)
        await lab.registry.cancel(bob_thread.thread_id)
    with run_as(ALICE):
        await lab.registry.cancel(running.thread_id)


class _SlowSecretManagerClient(FakeSecretManagerClient):
    DELAY_S = 0.3

    def access_secret_version(self, request: dict[str, Any]) -> Any:
        time.sleep(self.DELAY_S)
        return super().access_secret_version(request)


async def _max_loop_stall(work: Any) -> float:
    worst = 0.0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal worst
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        await work
    finally:
        done.set()
        await tick
    return worst


async def test_a_slow_credential_store_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.fast_api_app import app

    configure_apps(monkeypatch)
    client = _SlowSecretManagerClient()
    store = SecretManagerCredentialStore(client=client, project_id="p")
    set_credential_store(store)

    blocked = await _max_loop_stall(
        _on_loop(lambda: store.get_all("cold@example.com"))
    )
    assert blocked >= client.DELAY_S * 0.9

    async def voice_tool(user: str) -> Any:
        with run_as(user):
            return await _connected_upstreams()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:

        async def work() -> list[Any]:
            return await asyncio.gather(
                *(api.get("/api/v1/config") for _ in range(3)),
                *(voice_tool(f"voice-{n}@example.com") for n in range(3)),
            )

        started = time.perf_counter()
        stall = await _max_loop_stall(work())
        elapsed = time.perf_counter() - started

    assert elapsed >= client.DELAY_S, "the slow store was really consulted"
    assert stall < client.DELAY_S / 2, (
        f"the event loop stalled for {stall:.3f}s"
    )


async def _on_loop(fn: Any) -> None:
    await asyncio.sleep(0.02)
    fn()


@pytest.fixture
def rotating_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[str]]:
    configure_apps(monkeypatch, "google_workspace")
    used: list[str] = []
    valid = {"current": "r2"}
    fallback = integrations_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            token = parse_qs(request.content.decode())["refresh_token"][0]
            used.append(token)
            if token != valid["current"]:
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": "Refresh token revoked",
                    },
                )
            valid["current"] = "r3"
            return httpx.Response(
                200,
                json={
                    "access_token": "access-3",
                    "refresh_token": "r3",
                    "expires_in": 3600,
                },
            )
        return await fallback.handle_async_request(request)

    set_default_http_transport(httpx.MockTransport(handler))
    yield used


async def test_a_refresh_token_rotated_by_another_instance_is_reread_and_retried(
    rotating_workspace: list[str],
):
    client = FakeSecretManagerClient()
    this_instance = SecretManagerCredentialStore(client=client, project_id="p")
    other_instance = SecretManagerCredentialStore(client=client, project_id="p")
    set_credential_store(this_instance)
    this_instance.set_many(ALICE, {"GOOGLE_WORKSPACE_REFRESH_TOKEN": "r1"})
    other_instance.set_many(ALICE, {"GOOGLE_WORKSPACE_REFRESH_TOKEN": "r2"})

    with run_as(ALICE):
        assert await ensure_fresh_access_token("google_workspace") == "access-3"

    assert rotating_workspace == ["r1", "r2"]
    secret = f"projects/p/secrets/{_secret_id(ALICE)}"
    assert client.blob(secret) == {
        "GOOGLE_WORKSPACE_ACCESS_TOKEN": "access-3",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN": "r3",
    }


async def test_a_revoked_refresh_token_is_not_retried(
    rotating_workspace: list[str],
):
    client = FakeSecretManagerClient()
    store = SecretManagerCredentialStore(client=client, project_id="p")
    set_credential_store(store)
    store.set_many(ALICE, {"GOOGLE_WORKSPACE_REFRESH_TOKEN": "r-revoked"})

    with run_as(ALICE):
        assert await ensure_fresh_access_token("google_workspace") is None

    assert rotating_workspace == ["r-revoked"]


def _secret_id(user: str) -> str:
    from app.auth.credential_store import user_secret_id

    return user_secret_id(user)


async def test_a_deployed_service_warns_when_its_thread_store_is_sqlite(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.fast_api_app import warn_if_thread_store_is_ephemeral

    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert not warn_if_thread_store_is_ephemeral(), "local SQLite is expected"

    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    with caplog.at_level("WARNING"):
        assert warn_if_thread_store_is_ephemeral()
    assert "enable_cloud_sql" in caplog.text

    monkeypatch.setenv(
        "THREAD_DB_URL", "postgresql://u:p@/voice_agent?host=/cloudsql/x"
    )
    assert not warn_if_thread_store_is_ephemeral(), "Cloud SQL is durable"
