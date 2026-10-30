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
"""Maintenance task weather limits and physical assumptions.

Every number the agent uses to decide whether an hour is workable lives here,
so an operations team can replace them with the limits from their own crane
lift plans, OEM manuals and site safety rules.

The defaults are ILLUSTRATIVE planning limits, not manufacturer or regulatory
values. Override them with `TASK_LIMITS_JSON` (a path to a JSON file, or an
inline JSON object) whose keys are task types and whose values replace any of
the fields of `TaskLimits`, for example::

    {"main_component_lift": {"max_wind_ms": 9.0, "min_hours": 10}}
"""

import dataclasses
import json
import os
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class TaskLimits:
    """Weather limits for one type of turbine maintenance work."""

    description: str
    # Highest acceptable mean wind speed at hub height, m/s.
    max_wind_ms: float
    # Highest acceptable gust at hub height, m/s (estimated, see GUST_FACTOR).
    max_gust_ms: float
    # Highest acceptable 1-hour precipitation, mm.
    max_precip_mm: float
    # Number of consecutive workable hours the job needs.
    min_hours: int
    # Restrict windows to local working hours [work_start_hour, work_end_hour).
    daylight_only: bool = True
    work_start_hour: int = 7
    work_end_hour: int = 19
    # Probability of a fully workable window at or above which the window is
    # flagged as meeting the go threshold.
    go_probability: float = 0.8

    def effective_wind_limit(self, gust_factor: float) -> float:
        """Hub-height mean wind limit that also respects the gust limit."""
        return min(self.max_wind_ms, self.max_gust_ms / gust_factor)


DEFAULT_TASKS: dict[str, TaskLimits] = {
    "main_component_lift": TaskLimits(
        description=(
            "Main crane lift of a blade, hub or gearbox. Most wind-sensitive"
            " job; needs a long, calm, dry daylight window."
        ),
        max_wind_ms=10.0,
        max_gust_ms=14.0,
        max_precip_mm=0.5,
        min_hours=8,
        go_probability=0.8,
    ),
    "blade_repair": TaskLimits(
        description=(
            "Rope-access or platform blade repair (leading-edge erosion,"
            " laminate). Needs dry conditions for resin curing."
        ),
        max_wind_ms=12.0,
        max_gust_ms=15.0,
        max_precip_mm=0.1,
        min_hours=6,
        go_probability=0.7,
    ),
    "nacelle_service": TaskLimits(
        description=(
            "Climb and scheduled service inside the nacelle. Limited by safe"
            " tower climb and hub access."
        ),
        max_wind_ms=18.0,
        max_gust_ms=22.0,
        max_precip_mm=2.0,
        min_hours=4,
        go_probability=0.7,
    ),
}

# WeatherNext 3 does not publish a gust variable, so gusts are estimated from
# the hub-height mean wind with a gust factor (ratio of 3-second gust to
# hourly mean). Typical values are 1.2-1.5; use one derived from your SCADA
# or met-mast data. Set with GUST_FACTOR (default in .env.example).
#
# A power-law shear exponent moves WeatherNext's 100 m wind speed to each
# site's hub height: v_hub = v_100 * (hub_height / 100) ** alpha. Set with
# SHEAR_EXPONENT (default in .env.example).
REFERENCE_HEIGHT_M = 100.0


def require_env(name: str, value: str | None) -> str:
    """Returns `value` (read by the caller with `os.getenv(name)`) or fails.

    Defaults live in `.env.example`, which `app/__init__.py` loads (via
    `.env`) and the container image ships. Callers read the variable
    themselves so every environment read stays a plain `os.getenv("NAME")`.

    Raises:
        RuntimeError: If the value is unset or empty.
    """
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set; copy .env.example to .env"
        )
    return value


def gust_factor() -> float:
    """Returns the gust factor (GUST_FACTOR)."""
    return float(require_env("GUST_FACTOR", os.getenv("GUST_FACTOR")))


def shear_exponent() -> float:
    """Returns the hub-height shear exponent (SHEAR_EXPONENT)."""
    return float(require_env("SHEAR_EXPONENT", os.getenv("SHEAR_EXPONENT")))


def _load_overrides(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(Path(raw).read_text())


def load_tasks(overrides: str | None = None) -> dict[str, TaskLimits]:
    """Returns task limits with any overrides applied.

    Args:
        overrides: JSON object or path to a JSON file. Defaults to the
            TASK_LIMITS_JSON environment variable.

    Raises:
        ValueError: If an override sets an unknown field, or defines a new
            task without every required field.
    """
    if overrides is None:
        overrides = os.getenv("TASK_LIMITS_JSON") or ""
    tasks = dict(DEFAULT_TASKS)
    fields = {f.name for f in dataclasses.fields(TaskLimits)}
    for name, values in _load_overrides(overrides).items():
        unknown = set(values) - fields
        if unknown:
            raise ValueError(
                f"Unknown fields for task {name!r}: {sorted(unknown)}"
            )
        if name in tasks:
            tasks[name] = dataclasses.replace(tasks[name], **values)
        else:
            try:
                tasks[name] = TaskLimits(**values)
            except TypeError as e:
                raise ValueError(
                    f"Incomplete limits for new task {name!r}: {e}"
                ) from e
    return tasks
