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

"""Hermetic pytest fixtures isolating workspace, state, database, and network access."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from dotenv import dotenv_values

# Set before importing `app` so module-level initialization never reads ~/.env or ~/.adk-voice-agent.
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="adk-voice-agent-tests-"))
_MODEL_AND_RUNTIME_DEFAULTS = {
    k: v
    for k, v in dotenv_values(
        Path(__file__).resolve().parents[1] / ".env.example"
    ).items()
    if v and not (v.startswith("<TODO") and v.endswith(">"))
}
_HERMETIC_DEFAULT_KEYS = (
    "GOOGLE_GENAI_USE_ENTERPRISE",
    "GOOGLE_CLOUD_LOCATION",
    "LIVE_MODEL_LOCATION",
    "LIVE_MODEL",
    "GROUNDING_MODEL",
    "LIVE_VOICE_NAME",
    "CASCADE_STT_MODEL",
    "CASCADE_LLM_MODEL",
    "CASCADE_TTS_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTIGRAVITY_MODEL",
    "ANTIGRAVITY_EFFORT",
    "AGY_ADC_AUTH",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
for _key in _HERMETIC_DEFAULT_KEYS:
    if _key in _MODEL_AND_RUNTIME_DEFAULTS:
        os.environ[_key] = _MODEL_AND_RUNTIME_DEFAULTS[_key]
os.environ.update(
    {
        "VOICE_AGENT_LOAD_DOTENV": "0",
        "ADK_DISABLE_LOAD_DOTENV": "1",
        "VOICE_AGENT_SKIP_STARTUP_WARMUP": "1",
        "VOICE_AGENT_STATE_DIR": str(_SESSION_DIR / "state"),
        "VOICE_AGENT_DOTENV_PATH": str(_SESSION_DIR / ".env"),
        "VOICE_AGENT_WORKSPACE_ROOT": str(_SESSION_DIR / "workspaces"),
        "VOICE_AGENT_WORKSPACES_LOCAL_YAML": str(
            _SESSION_DIR / "workspaces.local.yaml"
        ),
        "THREAD_DB_URL": f"sqlite+aiosqlite:///{_SESSION_DIR / 'threads.db'}",
    }
)
for _key in ("DATABASE_URL", "SESSION_DB_URL"):
    os.environ.pop(_key, None)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.store.thread_store import ThreadStore  # noqa: E402
from app.threads.registry import ThreadRegistry  # noqa: E402
from app.workers.environment import LocalEnvironment  # noqa: E402
from tests.fakes import FakeAgent, Lab  # noqa: E402

# Exclude standalone git repo fixtures that carry their own `tests/` packages.
collect_ignore_glob = ["eval/fixtures/**"]

SHARED_WORKSPACE_FALLBACK = (
    Path(tempfile.gettempdir()) / "adk-voice-agent-workspaces"
)


@pytest.fixture(scope="session", autouse=True)
def _remove_session_dir() -> Iterator[None]:
    yield
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_workspace_and_project_files(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    from app.app_utils.http_client import set_default_http_transport
    from app.auth.identity import reset_iap_key_cache
    from tests.fake_integrations import integrations_transport

    isolated_ws = tmp_path_factory.mktemp("test-workspaces")
    isolated_cfg = tmp_path_factory.mktemp("test-config")
    for key in _HERMETIC_DEFAULT_KEYS:
        if key in _MODEL_AND_RUNTIME_DEFAULTS:
            monkeypatch.setenv(key, _MODEL_AND_RUNTIME_DEFAULTS[key])
    monkeypatch.setenv("VOICE_AGENT_WORKSPACE_ROOT", str(isolated_ws))
    monkeypatch.setenv("VOICE_AGENT_STATE_DIR", str(isolated_cfg / "state"))
    monkeypatch.setenv("VOICE_AGENT_DOTENV_PATH", str(isolated_cfg / ".env"))
    monkeypatch.setenv("VOICE_AGENT_LOAD_DOTENV", "0")
    monkeypatch.setenv("VOICE_AGENT_SKIP_STARTUP_WARMUP", "1")
    monkeypatch.setenv("VOICE_AGENT_ALLOWED_HOSTS", "testserver,test")
    monkeypatch.setenv(
        "THREAD_DB_URL", f"sqlite+aiosqlite:///{isolated_cfg / 'threads.db'}"
    )
    monkeypatch.setenv(
        "VOICE_AGENT_WORKSPACES_LOCAL_YAML",
        str(isolated_cfg / "workspaces.local.yaml"),
    )
    (isolated_cfg / "workspaces.local.yaml").write_text(
        "seed_repositories:\n  - name: voice-agent-fixture\n"
    )
    monkeypatch.setenv(
        "GIT_CONFIG_GLOBAL", str(isolated_cfg / "absent-gitconfig")
    )
    monkeypatch.setenv(
        "GIT_CONFIG_SYSTEM", str(isolated_cfg / "absent-gitconfig-system")
    )
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setattr(
        "app.auth.adc.adc_path", lambda: isolated_cfg / "gcloud-adc.json"
    )
    # Set then remove so values written to os.environ by a previous test's .env write are undone by monkeypatch teardown.
    from app.auth.github.provider import GITHUB_APP_SLUG_ENV
    from app.auth.providers import server_only_env_keys

    for key in (*server_only_env_keys(), GITHUB_APP_SLUG_ENV, "GITHUB_USER"):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    for key in (
        "VOICE_AGENT_AUTH_MODE",
        "VOICE_AGENT_DEV_USER_ID",
        "VOICE_AGENT_IAP_AUDIENCE",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_iap_key_cache()

    from app.memory.episodes import set_episode_store, set_summarizer
    from app.memory.store import set_memory_store

    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    set_memory_store(None)
    monkeypatch.delenv("VOICE_AGENT_CARRYOVER_MINUTES", raising=False)
    monkeypatch.delenv("VOICE_MODE", raising=False)
    set_episode_store(None)
    set_summarizer(None)
    from app.threads.summaries import fallback_summary, set_turn_summarizer

    async def first_paragraph(prompt: str, answer: str) -> str:
        return fallback_summary(answer)

    set_turn_summarizer(first_paragraph)

    from app.workers.factory import set_environment
    from app.workers.providers.last_known import reset_last_known
    from app.workers.providers.registry import reset_computer_providers

    monkeypatch.delenv("COMPUTER_PROVIDER", raising=False)
    set_environment(None)
    reset_computer_providers()
    reset_last_known()

    from app.auth.credential_store import (
        set_credential_store,
        set_secret_manager_client,
    )
    from tests.fakes import FakeSecretManagerClient

    monkeypatch.delenv("VOICE_AGENT_CREDENTIAL_STORE", raising=False)
    set_credential_store(None)
    set_secret_manager_client(FakeSecretManagerClient(), "test-project")

    set_default_http_transport(integrations_transport())

    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    import app.store.engine as engine_mod
    from app.app_utils.services import (
        get_artifact_service,
        get_session_service,
    )
    from app.store.user_settings import (
        UserSettingsStore,
        set_user_settings_store,
    )
    from app.threads.registry import set_thread_registry
    from app.user_settings import reset_user_settings_cache

    async def _reset_db_engines() -> None:
        if get_session_service.cache_info().currsize:
            svc = get_session_service()
            close = getattr(svc, "close", None)
            if callable(close):
                await close()
        get_session_service.cache_clear()
        get_artifact_service.cache_clear()
        await engine_mod.close_db()
        engine_mod._engine = None

    asyncio.run(_reset_db_engines())
    set_thread_registry(None)
    reset_user_settings_cache()
    settings_engine = create_async_engine(
        f"sqlite+aiosqlite:///{isolated_cfg / 'settings.db'}",
        poolclass=NullPool,
    )
    set_user_settings_store(UserSettingsStore(settings_engine))

    from app.context_cards import _cards, _dismissed
    from app.voice.focus import clear_focus

    _cards.clear()
    _dismissed.clear()
    clear_focus()
    yield
    _cards.clear()
    _dismissed.clear()
    clear_focus()
    set_memory_store(None)
    set_episode_store(None)
    set_summarizer(None)
    set_turn_summarizer(None)
    set_user_settings_store(None)
    reset_user_settings_cache()
    set_thread_registry(None)
    asyncio.run(settings_engine.dispose())
    asyncio.run(_reset_db_engines())
    set_default_http_transport(None)
    set_secret_manager_client(None)
    set_credential_store(None)


@pytest.fixture(autouse=True)
def _block_outbound_network(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail if a test opens a real HTTP connection (`httpx.MockTransport` is unaffected)."""
    import httpx

    real_async_connect = httpx.AsyncHTTPTransport.handle_async_request
    real_sync_connect = httpx.HTTPTransport.handle_request

    def _forbid(transport, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            f"Blocked real network call to {request.url} during tests. "
            "Inject an httpx.MockTransport or use the fake from tests/fake_integrations.py."
        )

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _forbid
    )
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _forbid)
    monkeypatch.setattr(
        "app.workers.environment._CONNECT_RETRY_DELAYS_S", (0.0, 0.0, 0.0)
    )

    yield

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", real_async_connect
    )
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", real_sync_connect
    )


@pytest.fixture
def iap_user(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    from tests.fake_integrations import IAP_AUDIENCE, iap_headers

    monkeypatch.setenv("VOICE_AGENT_AUTH_MODE", "iap")
    monkeypatch.setenv("VOICE_AGENT_IAP_AUDIENCE", IAP_AUDIENCE)
    return iap_headers("user@example.com")


@pytest_asyncio.fixture
async def lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Lab]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.store.engine import get_default_state_dir
    from app.threads import driver
    from app.threads.registry import set_thread_registry
    from app.workers.factory import set_environment

    monkeypatch.setattr(driver, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(driver, "PROGRESS_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(driver, "RETRY_DELAY_S", 0.0)
    env = LocalEnvironment(tmp_path / "workspace")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'threads.db'}"
    )
    store = ThreadStore(engine)
    registry = ThreadRegistry(store)
    set_environment(env)
    set_thread_registry(registry)
    agent = FakeAgent(tmp_path / "agent")
    for key, value in agent.env.items():
        monkeypatch.setenv(key, value)
    lab = Lab(env, agent, registry, store, get_default_state_dir())
    try:
        yield lab
    finally:
        from app.auth.identity import run_as

        for owner in {"local", *(h.owner for h in registry._threads.values())}:
            with run_as(owner):
                await registry.clear_all()
        set_thread_registry(None)
        set_environment(None)
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _no_writes_to_the_shared_workspace() -> Iterator[None]:
    before = (
        {p.name for p in SHARED_WORKSPACE_FALLBACK.iterdir()}
        if SHARED_WORKSPACE_FALLBACK.exists()
        else set()
    )

    yield

    after = (
        {p.name for p in SHARED_WORKSPACE_FALLBACK.iterdir()}
        if SHARED_WORKSPACE_FALLBACK.exists()
        else set()
    )
    new = after - before
    assert not new, (
        f"Tests wrote to the shared workspace fallback {SHARED_WORKSPACE_FALLBACK}: "
        f"{sorted(new)}. This directory persists across runs. Set "
        "VOICE_AGENT_WORKSPACE_ROOT to a tmp_path in the failing test."
    )
