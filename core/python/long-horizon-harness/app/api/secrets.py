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

"""/lha/secrets — per-user secret management. Values are write-only."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.auth import current_user_id
from app.secrets import SecretTooLargeError, get_secret_store
from app.secrets.dotenv import parse_dotenv

logger = logging.getLogger(__name__)

_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretValue(BaseModel):
    value: str


class ImportDotenvBody(BaseModel):
    content: str
    commit: bool = False


def attach_secrets_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.get("/secrets")
    async def list_secrets(
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        names = await get_secret_store().list_names(user_id)
        return {"secrets": names}

    @router.put("/secrets/{name}")
    async def put_secret(
        name: str,
        body: SecretValue,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        if not _VALID_NAME.match(name):
            raise HTTPException(
                status_code=400,
                detail="name must match [A-Za-z_][A-Za-z0-9_]* (a valid env var name)",
            )
        try:
            await get_secret_store().set_secret(user_id, name, body.value)
        except SecretTooLargeError as err:
            raise HTTPException(status_code=413, detail=str(err)) from err
        return {"name": name, "ok": True}

    @router.post("/secrets/import")
    async def import_dotenv(
        body: ImportDotenvBody,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        parsed = parse_dotenv(body.content)
        store = get_secret_store()
        existing = {s["name"] for s in await store.list_names(user_id)}
        # conflicts is advisory (cached list_names); commit re-merges from a fresh fetch in set_many.
        conflicts = sorted(k for k in parsed.entries if k in existing)
        if body.commit:
            try:
                await store.set_many(user_id, parsed.entries)
            except SecretTooLargeError as err:
                raise HTTPException(status_code=413, detail=str(err)) from err
        return {
            "valid": sorted(parsed.entries),
            "skipped": [
                {"lineno": s.lineno, "reason": s.reason} for s in parsed.skipped
            ],
            "conflicts": conflicts,
            "committed": body.commit,
        }

    @router.delete("/secrets/{name}")
    async def delete_secret(
        name: str,
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        deleted = await get_secret_store().delete_secret(user_id, name)
        return {"name": name, "deleted": deleted}

    app.include_router(router)
