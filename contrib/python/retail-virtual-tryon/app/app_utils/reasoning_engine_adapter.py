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

"""Serve the reasoning_engine ``{class_method, input}`` contract over HTTP."""

import inspect
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, encoders, responses, status
from vertexai.agent_engines.templates.adk import AdkApp

from app.app_utils import services


def _create_adk_app() -> AdkApp:
    from app.agent import app as adk_agent_app

    return AdkApp(
        app=adk_agent_app,
        session_service_builder=services.get_session_service,
        artifact_service_builder=services.get_artifact_service,
    )


def _get_adk_app(app: FastAPI) -> AdkApp:
    adk_app: AdkApp | None = getattr(app.state, "reasoning_engine_app", None)
    if adk_app is None:
        adk_app = _create_adk_app()
        app.state.reasoning_engine_app = adk_app
    return adk_app


def _allowed_methods(adk_app: AdkApp, *, streaming: bool) -> set[str]:
    ops = adk_app.register_operations()
    key = "stream" if streaming else ""
    return set(ops.get(key, ())) | set(ops.get(f"async_{key}".rstrip("_"), ()))


async def _parse_request(
    request: Request, adk_app: AdkApp, *, streaming: bool
) -> tuple[Callable[..., Any], dict[str, Any]]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON request body: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object.",
        )
    default_method = "async_stream_query" if streaming else "query"
    method_name = payload.get("class_method") or default_method
    kwargs = payload.get("input") or {}
    if not isinstance(method_name, str) or not isinstance(kwargs, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'class_method' must be a string and 'input' must be an object.",
        )
    if method_name not in _allowed_methods(adk_app, streaming=streaming):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported class_method: {method_name!r}",
        )
    return getattr(adk_app, method_name), kwargs


def attach_reasoning_engine_routes(app: FastAPI) -> None:
    """Register ``/api/reasoning_engine`` and ``/api/stream_reasoning_engine``."""

    @app.post("/api/reasoning_engine", response_model=None)
    async def reasoning_engine(request: Request) -> responses.JSONResponse:
        adk_app = _get_adk_app(app)
        method, kwargs = await _parse_request(request, adk_app, streaming=False)
        result = method(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return responses.JSONResponse(
            content={"output": encoders.jsonable_encoder(result)}
        )

    @app.post("/api/stream_reasoning_engine", response_model=None)
    async def stream_reasoning_engine(
        request: Request,
    ) -> responses.StreamingResponse:
        adk_app = _get_adk_app(app)
        method, kwargs = await _parse_request(request, adk_app, streaming=True)

        async def _chunks():
            stream = method(**kwargs)
            if inspect.isasyncgen(stream):
                async for item in stream:
                    yield (
                        json.dumps(
                            encoders.jsonable_encoder(item),
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            else:
                for item in stream:
                    yield (
                        json.dumps(
                            encoders.jsonable_encoder(item),
                            separators=(",", ":"),
                        )
                        + "\n"
                    )

        return responses.StreamingResponse(
            _chunks(), media_type="application/json"
        )
