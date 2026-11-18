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

"""Latency logs: one structured INFO line per timed phase, written one way everywhere."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("app.latency")


@contextmanager
def log_latency(phase: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log `latency phase=<phase> ... elapsed_s=<s>` on exit (`ok=False` on exception)."""
    start = time.perf_counter()
    ok = True
    try:
        yield fields
    except BaseException:
        ok = False
        raise
    finally:
        fields.update(elapsed_s=round(time.perf_counter() - start, 3))
        if not ok:
            fields["ok"] = False
        logger.info(
            "latency phase=%s %s",
            phase,
            " ".join(f"{key}={value}" for key, value in fields.items()),
            extra={"latency": {"phase": phase, **fields}},
        )
