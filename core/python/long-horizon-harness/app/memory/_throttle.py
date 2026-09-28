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

"""Per-session throttling for after-turn fork-style callbacks.

``auto_capture_callback`` and ``review_fork_callback`` fire on every parent
turn and each kick off (or perform) work that may end up calling Gemini.
On bursts of short user turns that's wasteful — the second invocation runs
while the first is still in flight against a near-identical snapshot.

``try_claim`` enforces two limits, keyed per fork type in session state:

* a cooldown window (``_FORK_COOLDOWN_SECONDS``, default 120s) between
  consecutive runs of the same type, and
* a per-session cap (``_FORK_PER_SESSION_CAP``, default 50) as a safety
  valve against runaway loops.

Set ``LHA_FORK_COOLDOWN=0`` to disable the cooldown (the cap still
applies) — used by evalsets that need every-turn behavior to score the
underlying logic without throttling noise.

``flush_fork.spawn_flush_fork`` and ``dream_review.request_dream_review``
are intentionally NOT routed through ``try_claim``: flush is already
rate-limited by compaction itself (it fires once per compaction, which
``compaction_interval=8`` in ``app/agent.py`` bounds to roughly one per
8 invocations — so a long session flushes repeatedly, just never per-turn),
and dream-review is on-demand. A per-turn cooldown is not the right shape
for either.
"""

from __future__ import annotations

import os
import time
from typing import Any

_THROTTLE_STATE_KEY = "_fork_throttle"
_FORK_COOLDOWN_SECONDS = 120.0
_FORK_PER_SESSION_CAP = 50
_FORK_COOLDOWN_ENV = "LHA_FORK_COOLDOWN"


def _cooldown_seconds() -> float:
    raw = os.environ.get(_FORK_COOLDOWN_ENV)
    if raw is None:
        return _FORK_COOLDOWN_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _FORK_COOLDOWN_SECONDS


def try_claim(state: Any, fork_type: str) -> bool:
    """Try to claim a fork slot for ``fork_type`` on this turn.

    Returns True (and records the run in ``state``) when the cooldown has
    elapsed and the per-session cap is not yet hit. Returns False (state
    unchanged) otherwise. Callers should treat False as "skip this turn".
    """
    if state is None:
        return True

    now = time.time()
    raw = state.get(_THROTTLE_STATE_KEY)
    throttle: dict[str, Any] = raw if isinstance(raw, dict) else {}

    entry = throttle.get(fork_type)
    if not isinstance(entry, dict):
        entry = {}
    try:
        count = int(entry.get("count", 0) or 0)
        last_at = float(entry.get("last_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        count = 0
        last_at = 0.0

    if count >= _FORK_PER_SESSION_CAP:
        return False

    cooldown = _cooldown_seconds()
    if cooldown > 0 and last_at > 0 and (now - last_at) < cooldown:
        return False

    throttle[fork_type] = {"count": count + 1, "last_at": now}
    state[_THROTTLE_STATE_KEY] = throttle
    return True


__all__ = [
    "try_claim",
]
