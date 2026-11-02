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

"""Memory guard for long, memory-capped investigation runs.

Investigations run inside a memory-capped Cloud Run job; a run that
accumulates too much (many evaluated cases, large resolved content) can be
OOM-killed with no Python traceback. `MemoryGuard` watches the container's
cgroup-charged memory and signals when to stop accumulating, before the
limit is hit.
"""

from __future__ import annotations

from pathlib import Path

# cgroup v2 (unified) then v1 fallbacks for charged usage and the hard limit.
_CGROUP_V2_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_CURRENT = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
_CGROUP_V1_MAX = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

# Absolute headroom (MB) to keep free below the cgroup limit.
DEFAULT_MEMORY_RESERVE_MB = 400.0


def _read_int(path: Path) -> int | None:
    """Reads a single integer from a cgroup file.

    Args:
        path: Path to the cgroup file.

    Returns:
        Parsed integer value, or None if unreadable or set to 'max'.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text in ("", "max"):  # v2 uses the literal "max" for "no limit"
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_cgroup_current_mb() -> float | None:
    """Returns the container's currently charged memory in megabytes.

    Returns:
        Charged memory in MB rounded to one decimal place, or None if unavailable.
    """
    raw = _read_int(_CGROUP_V2_CURRENT) or _read_int(_CGROUP_V1_CURRENT)
    return round(raw / 1024 / 1024, 1) if raw is not None else None


def read_cgroup_limit_mb() -> float | None:
    """Returns the container's enforced memory limit in megabytes.

    A v1 "no limit" sentinel (a near-max integer) is treated as no limit.

    Returns:
        Enforced memory limit in MB rounded to one decimal place, or None if unlimited.
    """
    raw = _read_int(_CGROUP_V2_MAX) or _read_int(_CGROUP_V1_MAX)
    if raw is None or raw > (1 << 62):  # v1 "unlimited" is a near-max int
        return None
    return round(raw / 1024 / 1024, 1)


def render_memory_summary() -> str:
    """Renders a compact one-line memory summary for logging.

    Returns:
        Formatted summary string (e.g. "cgroup 120 MB/512 MB" or "unavailable").
    """
    current = read_cgroup_current_mb()
    if current is None:
        return "unavailable"
    limit = read_cgroup_limit_mb()
    return f"cgroup {current:.0f} MB" + (f"/{limit:.0f} MB" if limit else "")


class MemoryGuard:
    """Signals when to stop accumulating to avoid an OOM-kill.

    ``baseline_mb`` is the memory at the investigation's start. It is passed
    in (captured once before the run) so the multi-turn and single-turn
    scopes -- which use separate guards -- measure growth from the same
    start. The memory grown since the baseline is the data collected; a
    later node copies it in full, so the guard reserves room for a second
    copy. It stops when:

        current + (current - baseline) + reserve  >=  limit.
    """

    def __init__(
        self,
        reserve_mb: float = DEFAULT_MEMORY_RESERVE_MB,
        baseline_mb: float | None = None,
    ) -> None:
        self._reserve_mb = reserve_mb
        self._baseline_mb = baseline_mb

    def should_stop(self) -> bool:
        current = read_cgroup_current_mb()
        limit = read_cgroup_limit_mb()
        if current is None or limit is None or limit <= 0:
            return False
        if self._baseline_mb is None:
            self._baseline_mb = current
        growth = current - self._baseline_mb
        return current + growth + self._reserve_mb >= limit
