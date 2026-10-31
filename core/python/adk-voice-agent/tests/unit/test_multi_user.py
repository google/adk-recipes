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

"""Threads, briefings, live updates, context cards, and sandboxes are per user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.auth.identity import current_user_id, run_as
from app.context_cards import active_context_cards, record_context_card
from app.threads.model import EventKind, ThreadHandle, ThreadState
from app.threads.registry import ThreadNotFound
from app.tools.thread_tools import cancel_thread, list_threads, read_thread
from app.voice.briefing import build_thread_briefing, collect_missed_milestones
from app.voice.thread_updates import ThreadUpdates
from app.workers import factory
from app.workers.environment import LocalEnvironment, SandboxEnvironment
from app.workers.factory import sandbox_owner
from app.workers.providers.common import is_owner_id
from app.workers.providers.static import StaticProvider
from tests.fake_integrations import iap_headers
from tests.fakes import GOAL, REPO, Lab, seed_thread

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"


async def _dispatch_as(lab: Lab, user: str, **kwargs: Any) -> ThreadHandle:
    """Dispatch as `user`; the thread waits at a permission gate only its owner can act on."""
    lab.agent.script({"steps": [{"run": "git push origin main"}]}, {})
    with run_as(user):
        return await lab.dispatch(**kwargs)


def _finished(thread_id: str, owner: str, message: str) -> ThreadHandle:
    handle = ThreadHandle(
        thread_id=thread_id,
        goal=f"{owner}'s goal",
        project=f"code/{REPO}",
        harness="claude",
        branch=f"agent/{thread_id}",
        owner=owner,
        state=ThreadState.COMPLETED,
        result_text=message,
    )
    handle.record(EventKind.COMPLETED, message)
    return handle


# ---- the registry -------------------------------------------------------------


async def test_a_user_sees_and_acts_on_only_their_own_threads(lab: Lab):
    alice_thread = await _dispatch_as(lab, ALICE)
    bob_thread = await _dispatch_as(lab, BOB)
    assert alice_thread.owner == ALICE and bob_thread.owner == BOB
    assert alice_thread.state is ThreadState.AWAITING_PERMISSION

    with run_as(BOB):
        assert [t.thread_id for t in await lab.registry.list_all()] == [
            bob_thread.thread_id
        ]
        assert await lab.registry.get(alice_thread.thread_id) is None
        for action in (
            lab.registry.require,
            lab.registry.approve,
            lab.registry.cancel,
            lab.registry.dismiss,
        ):
            with pytest.raises(ThreadNotFound):
                await action(alice_thread.thread_id)
        with pytest.raises(ThreadNotFound):
            await lab.registry.steer(
                alice_thread.thread_id, "Do it differently"
            )
        assert await lab.registry.clear_all() == 1

    assert alice_thread.state is ThreadState.AWAITING_PERMISSION
    assert not alice_thread.dismissed
    with run_as(ALICE):
        assert [t.thread_id for t in await lab.registry.list_all()] == [
            alice_thread.thread_id
        ], "clearing is per user"
    with run_as(BOB):
        assert await lab.registry.list_all() == []


async def test_the_store_lists_threads_per_owner_after_a_restart(lab: Lab):
    from app.threads.registry import ThreadRegistry

    await seed_thread(lab.store, _finished("thread-alice01", ALICE, "Done A"))
    await seed_thread(lab.store, _finished("thread-bob0001", BOB, "Done B"))
    restarted = ThreadRegistry(lab.store)
    with run_as(ALICE):
        assert [t.thread_id for t in await restarted.list_all()] == [
            "thread-alice01"
        ]
        assert await restarted.get("thread-bob0001") is None
    with run_as(BOB):
        loaded = await restarted.get("thread-bob0001")
        assert loaded is not None and loaded.owner == BOB


async def test_the_driver_runs_as_the_thread_owner(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
):
    from app.threads import driver

    seen: list[tuple[str, str]] = []
    real_resolve = driver.resolve_environment

    async def resolve() -> Any:
        seen.append(("resolve", current_user_id()))
        return await real_resolve()

    def credential_env() -> dict[str, str]:
        # Runs in a worker thread, which must inherit the owner too.
        seen.append(("credentials", current_user_id()))
        return {}

    monkeypatch.setattr(driver, "resolve_environment", resolve)
    monkeypatch.setattr(lab.env, "credential_env", credential_env)

    thread = await _dispatch_as(lab, ALICE)
    assert seen and {user for _, user in seen} == {ALICE}
    assert {step for step, _ in seen} == {"resolve", "credentials"}

    # Approving resumes the thread; its next segments still run as its owner.
    seen.clear()
    with run_as(ALICE):
        await lab.registry.approve(thread.thread_id)
        await lab.settle(thread.thread_id)
    assert seen and {user for _, user in seen} == {ALICE}


# ---- voice tools, briefing, and live updates ------------------------------------


async def test_voice_tools_only_see_the_callers_threads(lab: Lab):
    alice_thread = await _dispatch_as(lab, ALICE)
    with run_as(BOB):
        assert alice_thread.thread_id not in await list_threads()
        assert "No thread with ID" in await read_thread(alice_thread.thread_id)
        assert "No thread with ID" in await cancel_thread(
            alice_thread.thread_id
        )
    assert alice_thread.state is ThreadState.AWAITING_PERMISSION
    with run_as(ALICE):
        assert alice_thread.thread_id in await list_threads()


async def test_the_briefing_is_per_user(lab: Lab):
    await seed_thread(
        lab.store, _finished("thread-alice01", ALICE, "Alice shipped")
    )
    await seed_thread(
        lab.store, _finished("thread-bob0001", BOB, "Bob shipped")
    )

    with run_as(ALICE):
        await collect_missed_milestones()
    with run_as(BOB):
        await collect_missed_milestones()
        bob_briefing = build_thread_briefing()
    with run_as(ALICE):
        alice_briefing = build_thread_briefing()

    assert "Alice shipped" in alice_briefing
    assert "Bob shipped" not in alice_briefing
    assert "Bob shipped" in bob_briefing
    assert "Alice shipped" not in bob_briefing
    with run_as(ALICE):
        # Each user's cursor moved only past their own milestones.
        assert await lab.registry.unseen_milestones() == []


class _LiveQueue:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_content(self, content: Any) -> None:
        self.sent.append(content.parts[0].text)


async def test_live_updates_reach_only_the_owners_session(lab: Lab):
    updates = ThreadUpdates()
    alice_live, bob_live = _LiveQueue(), _LiveQueue()
    with run_as(ALICE):
        updates.attach(lab.registry, alice_live)
    with run_as(BOB):
        updates.attach(lab.registry, bob_live)

    bob_thread = await _dispatch_as(lab, BOB)
    assert alice_live.sent == []
    assert any(bob_thread.thread_id in text for text in bob_live.sent)

    alice_thread = await _dispatch_as(lab, ALICE)
    assert any(alice_thread.thread_id in text for text in alice_live.sent)
    assert all(alice_thread.thread_id not in text for text in bob_live.sent)


async def test_context_cards_are_per_user():
    with run_as(ALICE):
        record_context_card(
            kind="calendar",
            title="Alice's calendar",
            subtitle="Today",
            brand_icon="calendar",
            bullets=["Standup at 9"],
            surface_id="a2ui-ctx-alice",
        )
        assert [c["surfaceId"] for c in active_context_cards()] == [
            "a2ui-ctx-alice"
        ]
    with run_as(BOB):
        assert active_context_cards() == []


# ---- REST, as two signed-in IAP users --------------------------------------------


@pytest_asyncio.fixture
async def api(iap_user: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    from app.fast_api_app import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_rest_and_state_are_per_signed_in_user(
    lab: Lab, api: httpx.AsyncClient
):
    alice, bob = iap_headers(ALICE), iap_headers(BOB)
    lab.agent.script({"steps": [{"hang": True}]})
    created = await api.post(
        "/api/v1/threads", json={"goal": GOAL, "repo": REPO}, headers=alice
    )
    assert created.status_code == 200, created.text
    thread_id = created.json()["thread_id"]

    alice_state = (await api.get("/api/v1/state", headers=alice)).json()
    assert [t["thread_id"] for t in alice_state["threads"]] == [thread_id]
    bob_state = (await api.get("/api/v1/state", headers=bob)).json()
    assert bob_state["threads"] == []
    assert all(thread_id not in str(s) for s in bob_state["a2ui_surfaces"])

    # Someone else's thread is indistinguishable from no thread.
    assert (
        await api.get(f"/api/v1/threads/{thread_id}", headers=bob)
    ).status_code == 404
    for action in ("approve", "cancel"):
        res = await api.post(
            f"/api/v1/threads/{thread_id}/{action}", headers=bob
        )
        assert res.status_code == 404, action
    steer = await api.post(
        f"/api/v1/threads/{thread_id}/message",
        json={"instruction": "Stop"},
        headers=bob,
    )
    assert steer.status_code == 404

    mine = await api.get(f"/api/v1/threads/{thread_id}", headers=alice)
    assert mine.status_code == 200 and mine.json()["state"] != "cancelled"
    with run_as(ALICE):
        await lab.registry.cancel(thread_id)


# ---- environments ------------------------------------------------------------------


async def test_local_workspaces_are_per_user(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPUTER_PROVIDER", "host")
    factory.set_environment(None)
    with run_as(ALICE):
        alice_env = (await factory.resolve_environment()).env
    with run_as(BOB):
        bob_env = (await factory.resolve_environment()).env
    assert isinstance(alice_env, LocalEnvironment)
    assert isinstance(bob_env, LocalEnvironment)
    assert alice_env.workspace_root != bob_env.workspace_root
    assert "alice" not in alice_env.workspace_root


async def test_sandbox_owner_ids_are_stable_and_not_identifying():
    # Pinned: the same id on every server and run, or users lose their computer.
    assert sandbox_owner(ALICE) == "u-ff8d9819fc0e12bf"
    assert is_owner_id(sandbox_owner(ALICE))
    assert sandbox_owner(ALICE) != sandbox_owner(BOB)
    value = sandbox_owner(ALICE)
    assert "alice" not in value and "@" not in value


async def test_each_user_gets_their_own_computer(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "COMPUTER_URL_TEMPLATE", "http://computer-{owner}.test:8080"
    )
    provider = StaticProvider()
    alice = await provider.ensure(ALICE)
    bob = await provider.ensure(BOB)

    assert isinstance(alice, SandboxEnvironment)
    assert isinstance(bob, SandboxEnvironment)
    assert alice.name != bob.name
    assert alice.base_url != bob.base_url
    assert sandbox_owner(ALICE) in alice.base_url
    assert sandbox_owner(BOB) in bob.base_url
    assert "@" not in alice.base_url and "alice" not in alice.base_url
    # The provider is stateless: the same user always reaches the same computer.
    again = await provider.ensure(ALICE)
    assert isinstance(again, SandboxEnvironment)
    assert again.base_url == alice.base_url
