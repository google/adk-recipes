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

"""GET /health and GET /api/status."""

import dataclasses

from fastapi import APIRouter

from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import get_settings

__all__ = ["router"]

router = APIRouter(tags=["Status"])

# Cloud Run's request size limit: larger inputs go through POST /api/uploads.
REQUEST_LIMIT_MB = 32


@router.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "healthy"}


@router.get("/api/status")
async def status() -> dict:
    """Service availability, the models in use and where data is kept.

    ``input_mode`` / ``output_mode`` are the deployment defaults
    (``GENMEDIA_INPUT`` / ``GENMEDIA_OUTPUT``); ``request_limit_mb`` is the
    largest request the deployment accepts.
    """
    models = dataclasses.asdict(get_models())
    settings = get_settings()
    return {
        "models": models,
        "input_mode": settings.input_mode,
        "output_mode": settings.output_mode,
        "request_limit_mb": REQUEST_LIMIT_MB,
    }
