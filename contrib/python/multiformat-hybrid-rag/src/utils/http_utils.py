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

"""Shared HTTP helpers for the orchestrator → Cloud Run service fanouts.

Both `src.document_preprocessing.preprocess` and
`src.chunking.chunk_and_index` dispatch one HTTP request per file
to a Cloud Run service via `ThreadPoolExecutor`. Cloud Run can return
HTTP 500 "no available instance" during cold-start bursts when the
orchestrator dispatches faster than the autoscaler can spin up
containers. Those responses come back with `latency=0` and never reach
the app — retrying after a short sleep almost always succeeds because
by then a new instance is ready.
"""

from __future__ import annotations

import random
import time

import requests


def post_with_retry(
    url: str,
    headers: dict,
    body: dict,
    timeout: int,
    attempts: int = 4,
    rate_limit_attempts: int = 20,
) -> dict:
    """POST with exponential backoff on 5xx, 429, and connection errors.

    - 5xx + ConnectionError + Timeout: retried up to `attempts` times,
      backoff 0.5s, 1s, 2s, 4s, ... capped at 30s.
    - 429 (rate limit): retried with backoff but does NOT count toward
      the attempt limit — Cloud Run platform 429s during burst dispatch
      are expected and should not exhaust retry budget for transient
      service errors. Bounded separately by `rate_limit_attempts` so a
      service stuck returning 429 cannot loop forever.
    - Other 4xx: raise immediately (non-retryable).
    """
    last_exc: Exception | None = None
    attempt = 0
    rate_limited = 0
    while attempt < attempts:
        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=timeout
            )
            response.raise_for_status()
            return response.json()
        except (
            requests.HTTPError,
            requests.ConnectionError,
            requests.Timeout,
        ) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if (
                isinstance(e, requests.HTTPError)
                and status is not None
                and status < 500
                and status != 429
            ):
                raise
            last_exc = e
            # Jittered backoff: each thread sleeps a different fraction of
            # the base wait (0.5x-1.5x) so 200 simultaneously-rate-limited
            # threads don't wake at the same instant and re-burst the
            # service. Same average delay, no thundering herd.
            wait = min(30.0, 0.5 * (2**attempt)) * (0.5 + random.random())
            if status == 429:
                # Don't burn an attempt on platform rate-limit; Cloud Run
                # will admit us once the autoscaler catches up. Capped
                # independently so a permanently rate-limited endpoint
                # cannot spin here indefinitely.
                rate_limited += 1
                if rate_limited >= rate_limit_attempts:
                    raise
                time.sleep(wait)
                continue
            attempt += 1
            if attempt < attempts:
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]
