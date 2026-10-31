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

"""`/me`: the signed-in user, which the web client uses for ADK sessions and `/run_live`."""

from __future__ import annotations

from fastapi import APIRouter

from app.auth.identity import (
    AuthMode,
    acting_user_id,
    current_user_id,
    get_auth_mode,
)

router = APIRouter()

IAP_SIGN_OUT_URL = "/_gcp_iap/clear_login_cookie"


def account_summary() -> dict[str, str | None]:
    mode = get_auth_mode()
    return {
        "user_id": acting_user_id(),
        "auth_mode": mode.value,
        "sign_out_url": IAP_SIGN_OUT_URL if mode is AuthMode.IAP else None,
    }


@router.get("/me")
async def get_me() -> dict[str, str]:
    return {"user_id": current_user_id(), "auth_mode": get_auth_mode().value}
