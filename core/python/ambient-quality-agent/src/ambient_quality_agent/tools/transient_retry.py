# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Retry wrapper for per-case callables subject to transient failures.

Grading cases against external services, judges, or models can encounter
transient errors. Retrying these failures prevents cases from being marked
as errored and distorting the evaluated sample size.
"""

from __future__ import annotations

import copy
import functools
import logging
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

MAX_TRIES = 5
MAX_ELAPSED_SECONDS = 120.0
MAX_WAIT_SECONDS = 8.0
"""Cap on retry sleep intervals in seconds.

Produces exponential backoff delays of 2, 4, 8, and 8 seconds with full jitter.
"""

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_TRANSIENT_ERRORS = (ConnectionError, TimeoutError)


def wrap_with_transient_retry(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Retries a callable on transient failures, re-raising non-transient errors.

    The wrapped callable executes the following sequence:
    1. Deep-copies the input instance so prior failed attempts cannot corrupt state.
    2. Invokes ``fn`` with the copied instance.
    3. Re-raises immediately if the error is non-transient, attempts are exhausted,
       or the maximum elapsed time is exceeded.
    4. Sleeps for a jittered backoff interval before the next attempt.

    Args:
        fn: A single-argument callable to execute with retries. Must be free of
            external side effects across multiple attempts.

    Returns:
        A wrapped callable that executes ``fn`` with transient retry logic.
    """

    @functools.wraps(fn)
    def run(instance: Any) -> Any:
        started = time.monotonic()
        for attempt in range(1, MAX_TRIES + 1):
            try:
                return fn(copy.deepcopy(instance))
            except Exception as exc:
                spent = time.monotonic() - started
                if (
                    attempt == MAX_TRIES
                    or spent >= MAX_ELAPSED_SECONDS
                    or not _is_transient(exc)
                ):
                    raise
                logger.info(
                    "%s failed transiently (attempt %d/%d): %s",
                    getattr(fn, "__name__", "<callable>"),
                    attempt,
                    MAX_TRIES,
                    exc,
                )
                # Full jitter avoids synchronized retries against shared endpoints.
                time.sleep(
                    random.uniform(0, min(MAX_WAIT_SECONDS, 2.0**attempt))  # noqa: S311 - retry backoff jitter; non-cryptographic
                )
        raise AssertionError("unreachable")

    return run


def _is_transient(exc: BaseException | None) -> bool:
    """Determines whether an exception represents a retryable transient failure.

    Inspects both ``__cause__`` and ``__context__`` chains:
    1. Checks if the exception matches known transient error types.
    2. Checks if the extracted HTTP status code matches retryable codes.
    3. Traverses chained causes and contexts until a match is found or the chain ends.

    Args:
        exc: The exception to inspect, or None.

    Returns:
        True if the exception or any chained cause is transient, False otherwise.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, _TRANSIENT_ERRORS):
            return True
        if _extract_status_code(exc) in _RETRYABLE_STATUS_CODES:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _extract_status_code(exc: BaseException) -> int | None:
    """Extracts the HTTP status code from an exception, if present.

    Args:
        exc: The exception to inspect.

    Returns:
        The integer status code if found, or None.
    """
    code = (
        getattr(exc, "code", None)
        or getattr(exc, "status_code", None)
        # Check attached response objects for libraries such as requests and httpx.
        or getattr(getattr(exc, "response", None), "status_code", None)
    )
    return code if isinstance(code, int) else None
