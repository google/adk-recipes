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

"""Serve the reasoning_engine ``{class_method, input}`` contract over HTTP.

Exists to guarantee support for the Vertex AI Console Playground and Gemini
Enterprise (via ADK registration), which both invoke the engine through this
contract. Agent Engine forwards calls to ``/api/reasoning_engine`` (sync) and
``/api/stream_reasoning_engine`` (streaming); dispatch is limited to the
:class:`AdkApp` ``register_operations()`` methods so the wire output matches a
packaged Agent Engine.

Derived from the make-python-recipe-deployable template. The one
difference: the runtime is this recipe's own AgentEngineApp from
agent_engine_app.py, so a container deployment exposes the same
operations (including register_feedback) as a source deployment.
"""

import inspect
import json

from fastapi import FastAPI, HTTPException, Request, encoders, responses, status


def attach_reasoning_engine_routes(app: FastAPI) -> None:
    """Register reasoning_engine routes that dispatch to AgentEngineApp."""
    runtime = None
    streaming_methods: set[str] = set()
    sync_methods: set[str] = set()

    def get_runtime():
        nonlocal runtime, streaming_methods, sync_methods
        if runtime is None:
            from genmedia4commerce.agent_engine_app import agent_engine

            if agent_engine is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="AgentEngineApp failed to initialize; see startup logs.",
                )
            candidate = agent_engine
            candidate.set_up()
            operations = candidate.register_operations()
            streaming_methods = set(operations.get("stream", [])) | set(
                operations.get("async_stream", [])
            )
            sync_methods = set(operations.get("", [])) | set(
                operations.get("async", [])
            )
            # Published only after set_up() succeeds, so a failed one is retried.
            runtime = candidate
        return runtime

    def resolve_method(body: object, *, streaming: bool):
        class_method = (
            body.get("class_method") if isinstance(body, dict) else None
        )
        if not class_method:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request body must be a JSON object with class_method.",
            )
        rt = get_runtime()
        allowed = streaming_methods if streaming else sync_methods
        if class_method not in allowed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unsupported reasoning_engine method: {class_method!r}",
            )
        return getattr(rt, class_method)

    @app.post("/api/stream_reasoning_engine")
    async def stream_query(request: Request) -> responses.StreamingResponse:
        body = await request.json()
        method = resolve_method(body, streaming=True)

        async def generator():
            # `streaming_methods` merges the registry's SYNC `stream` bucket
            # with its `async_stream` one, so this is either a plain generator
            # or an async one. A plain generator has no `__aiter__`, and
            # `async for` over it raises TypeError at request time — invisible
            # until someone actually streams. Duck-type the object rather than
            # the callable, so a sync method returning an async iterable also
            # works. The sync route below draws the same distinction for the
            # `""` and `async` buckets via iscoroutinefunction.
            stream = method(**(body.get("input") or {}))
            if hasattr(stream, "__aiter__"):
                async for event in stream:
                    yield json.dumps(event) + "\n"
            else:
                for event in stream:
                    yield json.dumps(event) + "\n"

        return responses.StreamingResponse(
            content=generator(), media_type="application/json"
        )

    @app.post("/api/reasoning_engine")
    async def query(request: Request) -> responses.JSONResponse:
        body = await request.json()
        method = resolve_method(body, streaming=False)
        kwargs = body.get("input") or {}
        output = (
            await method(**kwargs)
            if inspect.iscoroutinefunction(method)
            else method(**kwargs)
        )
        return responses.JSONResponse(
            content=encoders.jsonable_encoder({"output": output})
        )
