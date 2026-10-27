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

"""Run-scoped logging.

An investigation touches many modules; correlating their logs by run is
essential. Rather than thread the ``run_id`` through every call, it is stored
in a `contextvars.ContextVar` for the duration of the run and injected into
every `logging.LogRecord` by a root-logger filter -- so any module's
``logger.info(...)`` automatically carries the id. The filter prepends
``[run_id=...]`` to the message so it shows regardless of the handler's
formatter.

Usage::

    install_run_id_logging()            # once, at process start
    with tag_logs_with_run_id(run_id):  # around the whole run
        ...                             # all logs here are tagged
"""

from __future__ import annotations

import contextvars
import functools
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from ambient_quality_agent.core._memory import render_memory_summary

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aqa_run_id", default=""
)


_installed = False


def install_run_id_logging() -> None:
    """Tags every log record during a run with its run_id and cgroup memory."""

    global _installed
    if _installed:
        return
    base_factory = logging.getLogRecordFactory()

    def build_tagged_record(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        run_id = _run_id.get()
        if run_id:
            record.msg = (
                f"[run_id={run_id} mem={render_memory_summary()}] {record.msg}"
            )
        return record

    logging.setLogRecordFactory(build_tagged_record)
    _installed = True


@contextmanager
def tag_logs_with_run_id(run_id: str) -> Iterator[None]:
    """Tags all logs emitted within the block with ``run_id``.

    Args:
        run_id: Investigation run identifier to inject into log messages.

    Yields:
        None.
    """
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


def log_node_run(
    name: str,
) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
    """Decorates a workflow node function to log its start and completion.

    Args:
        name: Name of the workflow node.

    Returns:
        Decorator wrapping the target node callable.
    """

    def decorate(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        logger = logging.getLogger(getattr(fn, "__module__", __name__))

        @functools.wraps(fn)
        def run_with_logging(ctx: Any) -> Any:
            logger.info("node %r start", name)
            started = time.monotonic()
            try:
                result = fn(ctx)
            except BaseException:
                logger.exception(
                    "node %r failed after %.1fs",
                    name,
                    time.monotonic() - started,
                )
                raise
            logger.info(
                "node %r done in %.1fs", name, time.monotonic() - started
            )
            return result

        return run_with_logging

    return decorate
