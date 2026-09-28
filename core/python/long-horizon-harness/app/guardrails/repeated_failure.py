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

"""``RepeatedFailureGuard`` — halts after N identical-signature tool failures."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

from app.guardrails.halt_consumer import latch_halt

_STREAK_STATE_KEY = "__repeated_failure_streak__"

# Mirrored into session.state so the system_prompt volatile tier can surface
# the most recent tool failure to the model on its next turn.
LAST_ERROR_STATE_KEY = "last_error"

# A streak entry older than this is considered stale and reset to zero.
# A transient failure at hour 2 should not still count toward the halt
# threshold at hour 5; the model has moved on to other work since then.
_DECAY_SECONDS = 600.0


def _error_text(tool_response: Mapping[str, Any]) -> str:
    error = tool_response.get("error")
    if error:
        return str(error)
    stderr = tool_response.get("stderr")
    exit_code = tool_response.get("exit_code")
    if exit_code is not None and exit_code != 0:
        return f"exit_code={exit_code}" + (f": {stderr}" if stderr else "")
    return str(dict(tool_response))


def _signature(tool_name: str, args: Mapping[str, Any] | None) -> str:
    canonical = json.dumps(
        args or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest}"


def _is_failure(tool_response: Any) -> bool:
    if not isinstance(tool_response, Mapping):
        return False
    if "error" in tool_response:
        return True
    exit_code = tool_response.get("exit_code")
    if exit_code is not None and exit_code != 0:
        return True
    if tool_response.get("success") is False:
        return True
    return False


class RepeatedFailureGuard:
    def __init__(self, *, threshold: int = 3) -> None:
        if threshold < 2:
            raise ValueError(
                f"threshold must be >= 2 (threshold=1 would halt on the first "
                f"failure); got {threshold}"
            )
        self.threshold = threshold

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        args: Mapping[str, Any] | None,
        tool_response: Any,
        tool_context: Any,
    ) -> None:
        tool_name = getattr(tool, "name", "") or ""
        signature = _signature(tool_name, args)
        now = time.time()
        streaks = _load_streaks(tool_context.state.get(_STREAK_STATE_KEY))
        _prune_stale(streaks, now)

        if _is_failure(tool_response):
            entry = streaks.get(signature)
            prior = entry["count"] if entry else 0
            count = prior + 1
            streaks[signature] = {"count": count, "last_failure_at": now}
            tool_context.state[_STREAK_STATE_KEY] = streaks
            tool_context.state[LAST_ERROR_STATE_KEY] = _error_text(
                tool_response
            )

            if count >= self.threshold:
                latch_halt(
                    tool_context.state,
                    f"{tool_name} failed {count} times in a row with the same "
                    "arguments — halting to avoid a tool loop.",
                )
        else:
            if signature in streaks:
                streaks.pop(signature, None)
                tool_context.state[_STREAK_STATE_KEY] = streaks
            # ADK's State has no .pop / del — overwrite with None so readers
            # using .get(...) treat it as absent.
            tool_context.state[LAST_ERROR_STATE_KEY] = None

    @staticmethod
    def reset(state: Any) -> None:
        """Clear the per-tool failure-streak counters.

        Without this, a halt cleared by ``acknowledge_halt`` re-fires on
        the next failure: the streak entry is already at ``threshold``,
        so the next failing call increments to ``threshold + 1`` and
        trips again.
        """
        state[_STREAK_STATE_KEY] = None


def _load_streaks(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sig, entry in raw.items():
        if isinstance(entry, dict) and "count" in entry:
            out[sig] = {
                "count": int(entry.get("count", 0)),
                "last_failure_at": float(entry.get("last_failure_at", 0.0)),
            }
    return out


def _prune_stale(streaks: dict[str, dict[str, Any]], now: float) -> None:
    expired = [
        sig
        for sig, entry in streaks.items()
        if now - entry.get("last_failure_at", 0.0) > _DECAY_SECONDS
    ]
    for sig in expired:
        streaks.pop(sig, None)
