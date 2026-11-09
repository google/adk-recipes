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

# ruff: noqa: S110
"""
Tokenomics SDK - Python Decorator for Custom Functions & Pipelines
"""

import functools
import inspect
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from adk_tokenomics_plugin.core.pricing import _safe_nonneg_int
from adk_tokenomics_plugin.core.tracker import TokenTracker

_GLOBAL_TRACKER: TokenTracker | None = None
_GLOBAL_TRACKER_LOCK = threading.Lock()


def _get_default_tracker() -> TokenTracker:
    global _GLOBAL_TRACKER
    if _GLOBAL_TRACKER is None:
        with _GLOBAL_TRACKER_LOCK:
            if _GLOBAL_TRACKER is None:
                _GLOBAL_TRACKER = TokenTracker()
    return _GLOBAL_TRACKER


def _has_usage_data(item: Any) -> bool:
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[1], Mapping)
    ):
        return True
    if (
        getattr(item, "usage_metadata", None) is not None
        or getattr(item, "usage", None) is not None
    ):
        return True
    return False


def _extract_chunk_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and (item[1] is None or isinstance(item[1], Mapping))
    ):
        return str(item[0] if item[0] is not None else "")
    txt = getattr(item, "text", None)
    if txt is not None:
        return str(txt)
    candidates = getattr(item, "candidates", None)
    if candidates and len(candidates) > 0:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if parts:
            return "".join(str(getattr(p, "text", "") or "") for p in parts)
    return ""


def _extract_usage_from_result(result: Any) -> tuple[str, int, int, int, int]:
    input_tokens = 0
    cached_tokens = 0
    output_tokens = 0
    thinking_tokens = 0

    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[1], Mapping)
    ):
        usage = result[1]
        tool_use_tok = _safe_nonneg_int(
            usage.get("tool_use_prompt_token_count")
            if usage.get("tool_use_prompt_token_count") is not None
            else (
                usage.get("toolUsePromptTokenCount")
                if usage.get("toolUsePromptTokenCount") is not None
                else usage.get("tool_use_tokens")
            )
        )
        cached_tokens = _safe_nonneg_int(
            usage.get("cached_tokens")
            if usage.get("cached_tokens") is not None
            else (
                usage.get("cached_content_token_count")
                if usage.get("cached_content_token_count") is not None
                else usage.get("cache_read_input_tokens")
            )
        )
        raw_prompt = (
            usage.get("prompt_tokens")
            if usage.get("prompt_tokens") is not None
            else (
                usage.get("prompt_token_count")
                if usage.get("prompt_token_count") is not None
                else usage.get("promptTokens")
            )
        )
        if raw_prompt is not None and usage.get("input_tokens") is None:
            prompt_tok = _safe_nonneg_int(raw_prompt) + tool_use_tok
            if prompt_tok > 0 and cached_tokens > prompt_tok:
                cached_tokens = prompt_tok
            input_tokens = max(0, prompt_tok - cached_tokens)
        else:
            input_tokens = (
                _safe_nonneg_int(usage.get("input_tokens")) + tool_use_tok
            )
        output_tokens = _safe_nonneg_int(
            usage.get("output_tokens")
            if usage.get("output_tokens") is not None
            else (
                usage.get("completion_tokens")
                if usage.get("completion_tokens") is not None
                else usage.get("candidates_token_count")
            )
        )
        thinking_tokens = _safe_nonneg_int(
            usage.get("thinking_tokens")
            if usage.get("thinking_tokens") is not None
            else (
                usage.get("reasoning_tokens")
                if usage.get("reasoning_tokens") is not None
                else usage.get("thoughts_token_count")
            )
        )
        resp_text = str(result[0] if result[0] is not None else "")
        return (
            resp_text,
            input_tokens,
            cached_tokens,
            output_tokens,
            thinking_tokens,
        )

    usage_meta = getattr(result, "usage_metadata", None)
    if usage_meta is None:
        usage_meta = getattr(result, "usage", None)
    if usage_meta is not None:

        def _field(obj: Any, *names: str) -> int:
            for n in names:
                val = (
                    obj.get(n)
                    if isinstance(obj, Mapping)
                    else getattr(obj, n, None)
                )
                if val is not None:
                    return _safe_nonneg_int(val)
            return 0

        prompt_tok = _field(
            usage_meta,
            "prompt_token_count",
            "promptTokens",
            "prompt_tokens",
            "input_tokens",
            "inputTokens",
        )
        tool_use_tok = _field(
            usage_meta,
            "tool_use_prompt_token_count",
            "toolUsePromptTokenCount",
            "tool_use_tokens",
        )
        prompt_tok += tool_use_tok
        cached_tokens = _field(
            usage_meta,
            "cached_content_token_count",
            "cachedContentTokenCount",
            "cacheReadTokens",
            "cached_tokens",
            "cache_read_input_tokens",
        )
        if (
            _field(
                usage_meta,
                "prompt_token_count",
                "promptTokens",
                "prompt_tokens",
            )
            == 0
            and _field(usage_meta, "input_tokens", "inputTokens") > 0
            and _field(usage_meta, "cache_read_input_tokens", "cacheReadTokens")
            > 0
        ):
            input_tokens = prompt_tok
        else:
            if prompt_tok > 0 and cached_tokens > prompt_tok:
                cached_tokens = prompt_tok
            input_tokens = max(0, prompt_tok - cached_tokens)
        output_tokens = _field(
            usage_meta,
            "candidates_token_count",
            "candidatesTokenCount",
            "completion_tokens",
            "completionTokens",
            "output_tokens",
            "outputTokens",
        )
        thinking_tokens = _field(
            usage_meta,
            "thoughts_token_count",
            "thoughtsTokenCount",
            "thinkingOutputTokens",
            "thinking_tokens",
            "reasoning_tokens",
            "reasoningTokens",
        )
        resp_text = _extract_chunk_text(result) or str(result)
        return (
            resp_text,
            input_tokens,
            cached_tokens,
            output_tokens,
            thinking_tokens,
        )

    return str(result), 0, 0, 0, 0


def _record_decorated_turn(
    tracker: TokenTracker | None,
    app_name: str,
    model_name: str,
    args: tuple[Any, ...],
    result: Any,
    start_time: float,
) -> None:
    try:
        duration_ms = max(1, math.ceil((time.time() - start_time) * 1000.0))
        resp_text, in_tok, cache_tok, out_tok, think_tok = (
            _extract_usage_from_result(result)
        )
        active_tracker = tracker or _get_default_tracker()
        active_tracker.record_turn(
            session_id="decorated_session",
            app_name=app_name,
            model_name=model_name,
            user_query=str(args[0]) if len(args) > 0 else "function_call",
            agent_response=resp_text[:1000],
            input_tokens=in_tok,
            cached_tokens=cache_tok,
            output_tokens=out_tok,
            thinking_tokens=think_tok,
            latency_ms=duration_ms,
        )
    except Exception:
        # Telemetry must never fail or swallow the caller's return value
        pass


def _record_decorated_stream_turn(
    tracker: TokenTracker | None,
    app_name: str,
    model_name: str,
    args: tuple[Any, ...],
    chunks: list[Any],
    last_usage_chunk: Any,
    start_time: float,
) -> None:
    try:
        duration_ms = max(1, math.ceil((time.time() - start_time) * 1000.0))
        in_tok = 0
        cache_tok = 0
        out_tok = 0
        think_tok = 0
        fallback_text = ""
        if last_usage_chunk is not None:
            fallback_text, in_tok, cache_tok, out_tok, think_tok = (
                _extract_usage_from_result(last_usage_chunk)
            )
        assembled_text = "".join(_extract_chunk_text(c) for c in chunks)
        resp_text = assembled_text if assembled_text else fallback_text
        active_tracker = tracker or _get_default_tracker()
        active_tracker.record_turn(
            session_id="decorated_session",
            app_name=app_name,
            model_name=model_name,
            user_query=str(args[0]) if len(args) > 0 else "function_call",
            agent_response=resp_text[:1000],
            input_tokens=in_tok,
            cached_tokens=cache_tok,
            output_tokens=out_tok,
            thinking_tokens=think_tok,
            latency_ms=duration_ms,
        )
    except Exception:
        pass


def _wrap_sync_generator_instance(
    gen: Any,
    tracker: TokenTracker | None,
    app_name: str,
    model_name: str,
    args: tuple[Any, ...],
    start_time: float,
):
    chunks: list[Any] = []
    last_usage_chunk: Any = None
    should_record = False
    try:
        while True:
            try:
                item = next(gen)
            except StopIteration as stop_exc:
                should_record = True
                return stop_exc.value
            chunks.append(item)
            if _has_usage_data(item):
                last_usage_chunk = item
            sent = yield item
            while sent is not None:
                try:
                    item = gen.send(sent)
                except StopIteration as stop_exc:
                    should_record = True
                    return stop_exc.value
                chunks.append(item)
                if _has_usage_data(item):
                    last_usage_chunk = item
                sent = yield item
    except GeneratorExit:
        if last_usage_chunk is not None:
            should_record = True
        raise
    finally:
        if should_record:
            _record_decorated_stream_turn(
                tracker,
                app_name,
                model_name,
                args,
                chunks,
                last_usage_chunk,
                start_time,
            )


async def _wrap_async_generator_instance(
    agen: Any,
    tracker: TokenTracker | None,
    app_name: str,
    model_name: str,
    args: tuple[Any, ...],
    start_time: float,
):
    chunks: list[Any] = []
    last_usage_chunk: Any = None
    should_record = False
    try:
        async for item in agen:
            chunks.append(item)
            if _has_usage_data(item):
                last_usage_chunk = item
            yield item
        should_record = True
    except GeneratorExit:
        if last_usage_chunk is not None:
            should_record = True
        raise
    finally:
        if should_record:
            _record_decorated_stream_turn(
                tracker,
                app_name,
                model_name,
                args,
                chunks,
                last_usage_chunk,
                start_time,
            )


def track_tokens(
    app_name: str = "custom_agent",
    model_name: str = "publishers/google/models/gemini-3.8-flash",
    tracker: TokenTracker | None = None,
):
    """
    Decorator to wrap any sync, async, sync-generator, or async-generator LLM invocation function and track tokenomics.

    Usage:
        @track_tokens(app_name="rag_search", model_name="publishers/google/models/gemini-3.8-flash")
        def ask_agent(prompt: str) -> str:
            return response
    """

    def decorator(func: Callable):
        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def async_gen_wrapper(*args, **kwargs):
                start_time = time.time()
                agen = func(*args, **kwargs)
                async for item in _wrap_async_generator_instance(
                    agen, tracker, app_name, model_name, args, start_time
                ):
                    yield item

            return async_gen_wrapper

        if inspect.isgeneratorfunction(func):

            @functools.wraps(func)
            def sync_gen_wrapper(*args, **kwargs):
                start_time = time.time()
                gen = func(*args, **kwargs)
                return (
                    yield from _wrap_sync_generator_instance(
                        gen, tracker, app_name, model_name, args, start_time
                    )
                )

            return sync_gen_wrapper

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                start_time = time.time()
                result = await func(*args, **kwargs)
                if inspect.isasyncgen(result):
                    return _wrap_async_generator_instance(
                        result, tracker, app_name, model_name, args, start_time
                    )
                if inspect.isgenerator(result):
                    return _wrap_sync_generator_instance(
                        result, tracker, app_name, model_name, args, start_time
                    )
                _record_decorated_turn(
                    tracker, app_name, model_name, args, result, start_time
                )
                return result

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = func(*args, **kwargs)
            if inspect.isasyncgen(result):
                return _wrap_async_generator_instance(
                    result, tracker, app_name, model_name, args, start_time
                )
            if inspect.isgenerator(result):
                return _wrap_sync_generator_instance(
                    result, tracker, app_name, model_name, args, start_time
                )
            _record_decorated_turn(
                tracker, app_name, model_name, args, result, start_time
            )
            return result

        return wrapper

    return decorator
