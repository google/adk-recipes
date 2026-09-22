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

"""CUJ-13: the scheduler endpoints reject everyone except Cloud Scheduler.

End-to-end through the real ``verify_cloud_scheduler_token`` dependency
(no bypass) with ``_verify_oauth2_token`` monkeypatched. Covers the
five failure modes plus the happy path.

Why each case matters in production:

* No header — most common attacker probe (random scanner).
* Malformed (no ``Bearer`` prefix) — sloppy client misconfiguration.
* Verifier raises — the real Google library throws on bad signature,
  expired token, or wrong audience. All collapse to one 401.
* Email not on allowlist — token is valid but issued to the wrong SA
  (e.g. someone in the same project ran ``gcloud auth print-identity-token``).
* Audience env unset — fail-closed: a misconfigured deploy must 500,
  not silently accept anything.
* Happy path — exact SA + audience expected by the Terraform.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


_AUDIENCE = "https://lha-123456789.us-central1.run.app"
_SCHEDULER_SA = "lha-scheduler@example.iam.gserviceaccount.com"


def _build_app() -> FastAPI:
    from horizon.scheduler import routine_tick_endpoint

    app = FastAPI()
    app.include_router(routine_tick_endpoint.router)
    return app


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from horizon.scheduler import auth as auth_mod
    from horizon.scheduler import routine_store

    routine_store.reset_routine_store()
    auth_mod._reset_bypass_warning_for_tests()
    monkeypatch.delenv("LHA_SCHEDULER_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("LHA_SCHEDULER_SA", _SCHEDULER_SA)
    yield
    routine_store.reset_routine_store()
    auth_mod._reset_bypass_warning_for_tests()


async def _post(
    app: FastAPI, headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t"
    ) as ac:
        return await ac.post("/scheduler/routine-tick", headers=headers or {})


async def test_missing_header_returns_401():
    resp = await _post(_build_app())
    assert resp.status_code == 401
    assert "bearer" in resp.json()["detail"].lower()


async def test_malformed_header_returns_401():
    resp = await _post(_build_app(), {"Authorization": "Token foo"})
    assert resp.status_code == 401


async def test_verifier_raises_returns_401(monkeypatch):
    from horizon.scheduler import auth as auth_mod

    def boom(token, audience):
        raise ValueError("invalid signature")

    monkeypatch.setattr(auth_mod, "_verify_oauth2_token", boom)

    resp = await _post(_build_app(), {"Authorization": "Bearer anything"})
    assert resp.status_code == 401
    assert "verification" in resp.json()["detail"].lower()


async def test_wrong_audience_returns_401(monkeypatch):
    from horizon.scheduler import auth as auth_mod

    def mimic_audience_mismatch(token, audience):
        # google.oauth2.id_token raises ValueError on aud mismatch.
        assert audience == _AUDIENCE, (
            "verifier should be called with the configured audience"
        )
        raise ValueError(f"Token has wrong audience for {audience}")

    monkeypatch.setattr(
        auth_mod, "_verify_oauth2_token", mimic_audience_mismatch
    )

    resp = await _post(_build_app(), {"Authorization": "Bearer aud-mismatch"})
    assert resp.status_code == 401


async def test_email_not_on_allowlist_returns_403(monkeypatch):
    from horizon.scheduler import auth as auth_mod

    def returns_intruder(token, audience):
        return {"email": "attacker@evil.example", "email_verified": True}

    monkeypatch.setattr(auth_mod, "_verify_oauth2_token", returns_intruder)

    resp = await _post(
        _build_app(), {"Authorization": "Bearer valid-but-wrong-caller"}
    )
    assert resp.status_code == 403
    assert "allowlist" in resp.json()["detail"].lower()


async def test_audience_unset_returns_500(monkeypatch):
    monkeypatch.delenv("LHA_SCHEDULER_AUDIENCE", raising=False)
    resp = await _post(_build_app(), {"Authorization": "Bearer anything"})
    assert resp.status_code == 500
    assert "audience" in resp.json()["detail"].lower()


async def test_happy_path_returns_200(monkeypatch):
    from horizon.scheduler import auth as auth_mod

    def returns_scheduler_sa(token, audience):
        assert audience == _AUDIENCE
        return {"email": _SCHEDULER_SA, "email_verified": True}

    monkeypatch.setattr(auth_mod, "_verify_oauth2_token", returns_scheduler_sa)

    resp = await _post(
        _build_app(), {"Authorization": "Bearer cloud-scheduler-oidc"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"fired": 0}


async def test_allowlist_case_insensitive(monkeypatch):
    from horizon.scheduler import auth as auth_mod

    monkeypatch.setenv("LHA_SCHEDULER_SA", _SCHEDULER_SA.upper())

    def returns_lower_email(token, audience):
        return {"email": _SCHEDULER_SA, "email_verified": True}

    monkeypatch.setattr(auth_mod, "_verify_oauth2_token", returns_lower_email)

    resp = await _post(_build_app(), {"Authorization": "Bearer case-check"})
    assert resp.status_code == 200
