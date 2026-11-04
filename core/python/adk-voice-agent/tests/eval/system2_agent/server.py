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

"""ASGI server for System 2 evals: ADK HTTP API for `system2_agent` plus `/api/v1/mcp`."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

from app.api.mcp import mcp_router

from .runner import install

EVAL_DIR = Path(__file__).resolve().parents[1]


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    install()
    yield
    from app.store.engine import close_db

    await close_db()


app = get_fast_api_app(agents_dir=str(EVAL_DIR), web=False, lifespan=lifespan)
app.include_router(mcp_router)
