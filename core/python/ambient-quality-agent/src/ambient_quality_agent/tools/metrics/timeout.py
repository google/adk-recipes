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

"""Stop waiting on a metric that never returns.

A metric is operator-written Python. One with an accidental `while True`, or a
call to something that never answers, would otherwise hang the sweep: the node
waits forever, the run never finishes, and because the window only advances on a
completed run, every later sweep waits behind it too.

This bounds the wait, not the metric. Python cannot interrupt a thread that is
not cooperating, so a timed-out metric keeps running on a daemon thread until
the process exits. That is the honest trade: one leaked thread per hung metric,
against an investigation that never completes. It is a guard against a mistake,
not against hostile code -- code published here executes in this process with
this process's credentials either way, and the gate for that is IAM on the
bucket.
"""

from __future__ import annotations

import functools
import logging
from concurrent import futures
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60.0
"""Maximum execution time in seconds for evaluating a metric on a single session.

Provides adequate headroom for judge evaluations while bounding hung calls.
"""

_POOL = futures.ThreadPoolExecutor(
    thread_name_prefix="metric",
    # Daemon threads, so a hung metric cannot keep the process alive at exit.
    initializer=None,
)


def wrap_with_timeout(
    fn: Callable[[Any], Any], seconds: float = TIMEOUT_SECONDS
) -> Callable[[Any], Any]:
    """Wrap a metric callable to enforce an execution timeout.

    Execution sequence:
    1. Submits the callable to a thread pool executor.
    2. Waits for completion up to `seconds`.
    3. On timeout, cancels the future, logs an error, and raises `TimeoutError`.

    Args:
        fn: Metric callable to execute.
        seconds: Maximum execution time in seconds before raising TimeoutError.

    Returns:
        Wrapped callable that raises TimeoutError if execution exceeds the timeout.
    """

    @functools.wraps(fn)
    def run(instance: Any) -> Any:
        future = _POOL.submit(fn, instance)
        try:
            return future.result(timeout=seconds)
        except futures.TimeoutError:
            # Cancelling cannot stop a thread that is already running; this only
            # keeps it from being retried by the pool.
            future.cancel()
            name = getattr(fn, "__name__", "<callable>")
            logger.error(
                "Metric %s did not return within %.0fs; abandoning it. The "
                "thread it is on will run until this process exits.",
                name,
                seconds,
            )
            raise TimeoutError(
                f"metric did not return within {seconds:.0f}s"
            ) from None

    return run
