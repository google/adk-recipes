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
"""Deterministic ensemble analysis: window probabilities, outlooks, crews.

No model is involved here. The workflow's function nodes call these
functions and the explain agents only narrate their results.

The input is any dataset that follows the forecast dataset contract
(`contract.py`); variables are found by CF `standard_name` and converted
with their `units`, so WeatherNext 3 and ECMWF AIFS ENS share this code.

Probability semantics follow the WeatherNext ensemble-analysis skill:

- A *step condition* (for example "hub wind <= 10 m/s") is evaluated for
  every member at every step. A missing (NaN) value never satisfies it, and
  steps that are missing for all members are left out.
- `prob_all_steps`: fraction of members for which the condition holds at
  every step of the window (temporal persistence). `prob_any_step`: at least
  one step. `peak_step_prob`: the highest single-step probability, with its
  valid time.
- The *joint condition* ANDs conditions within the same member, so the
  wind-to-rain and hour-to-hour correlation of each member is kept.

A maintenance window is workable for a member when the joint condition
"hub wind <= limit AND precipitation <= limit" holds at all of the window's
steps, so `probability_safe` is the joint `prob_all_steps`. Which steps
belong to a window depends on step semantics: instantaneous wind uses the
steps that bracket the window (the last step at or before its start through
the first step at or after its end); interval variables (precipitation
`avg` or `accum`) use every step whose interval overlaps the window. With
6-hourly data (AIFS ENS) a window therefore rests on few samples, and the
6-hour mean rain rate is compared with an hourly limit; the result says so
in `step_hours`.
"""

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import xarray as xr

from . import config, contract
from .forecast import Site

# Percentiles reported across members.
SUMMARY_PERCENTILES = (10, 50, 90)
_ONE_HOUR = timedelta(hours=1)


def percentile(values: np.ndarray, q: float) -> float:
    """Percentile across members, ignoring NaN (numpy's linear method)."""
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise ValueError("percentile of no finite values")
    return float(np.percentile(finite, q))


def hub_scale(
    site: Site, reference_height_m: float, shear_exponent: float | None = None
) -> float:
    """Power-law factor moving wind from `reference_height_m` to hub height."""
    alpha = (
        config.shear_exponent() if shear_exponent is None else shear_exponent
    )
    return (site.hub_height_m / reference_height_m) ** alpha


def local_time(t: datetime, site: Site) -> datetime:
    return t.astimezone(ZoneInfo(site.timezone))


def expected_hours(day: date, tz: ZoneInfo) -> float:
    """Length of a local calendar day in hours (23 or 25 on DST changes)."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    # Subtract in UTC: aware datetimes sharing a tzinfo subtract wall time.
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600


@dataclasses.dataclass
class SiteForecast:
    """Contract dataset reduced to the arrays the planner needs.

    Attributes:
        hub_wind: (member, step) hub-height wind speed, m/s.
        precip: (member, step) precipitation in `precip_units`: `mm/h` for
            rates (`avg`) or `mm` per step for accumulations (`accum`).
        precip_limit_scale: per-step factor turning a mm/h limit into
            `precip_units` (1 for rates, the step length in hours for
            accumulations).
    """

    site: Site
    init_time: datetime
    times: list[datetime]
    hub_wind: np.ndarray
    wind_reference_height_m: float
    wind_intervals: list[timedelta]
    precip: np.ndarray
    precip_units: str
    precip_step_type: str
    precip_intervals: list[timedelta]
    precip_limit_scale: np.ndarray
    title: str
    attribution: str

    @property
    def members(self) -> int:
        return int(self.hub_wind.shape[0])

    @classmethod
    def from_dataset(
        cls, ds: xr.Dataset, site: Site, shear_exponent: float | None = None
    ) -> "SiteForecast":
        """Reads wind and precipitation by `standard_name` and `units`.

        Raises:
            contract.ContractError: If the dataset breaks the contract or
                lacks a usable wind or precipitation variable.
        """
        contract.validate(ds)
        member_dim = contract.realization_dim(ds)
        wind = contract.find_variable(ds, contract.WIND_SPEED)
        if wind.attrs["step_type"] != "instant":
            raise contract.ContractError("wind_speed must be instantaneous")
        height = wind.attrs.get(contract.HEIGHT_ATTR)
        if height is None:
            raise contract.ContractError(
                f"wind_speed needs a {contract.HEIGHT_ATTR!r} attribute for the"
                " hub-height extrapolation"
            )
        scale = hub_scale(site, float(height), shear_exponent)
        hub = contract.to_units(wind, "m/s").transpose(member_dim, "lead_time")

        rate_vars = [
            v
            for v in ds.data_vars
            if ds[v].attrs.get("standard_name") in contract.PRECIP_RATE_NAMES
        ]
        if rate_vars:
            raw = contract.find_variable(ds, contract.PRECIP_RATE_NAMES)
            precip = contract.to_units(raw, "mm/h")
        else:
            raw = contract.find_variable(ds, contract.PRECIP_AMOUNT_NAMES)
            precip = contract.to_units(raw, "mm")
        step_type = raw.attrs["step_type"]
        if step_type not in ("avg", "accum"):
            raise contract.ContractError(
                f"precipitation step_type {step_type!r} is not supported"
            )
        intervals = contract.step_intervals(ds, raw)
        if precip.attrs["units"] == "mm/h":
            limit_scale = np.ones(len(intervals))
        else:
            limit_scale = np.array([i / _ONE_HOUR for i in intervals])
        return cls(
            site=site,
            init_time=contract.init_time(ds),
            times=contract.valid_times(ds),
            hub_wind=hub.values.astype(float) * scale,
            wind_reference_height_m=float(height),
            wind_intervals=contract.step_intervals(ds, wind),
            precip=precip.transpose(member_dim, "lead_time").values.astype(
                float
            ),
            precip_units=precip.attrs["units"],
            precip_step_type=step_type,
            precip_intervals=intervals,
            precip_limit_scale=limit_scale,
            title=str(ds.attrs.get("title", "")),
            attribution=str(ds.attrs.get("disclaimer", "")),
        )

    def bracketing_steps(self, start: datetime, end: datetime) -> list[int]:
        """Instantaneous steps from the last <= start to the first >= end."""
        before = [i for i, t in enumerate(self.times) if t <= start]
        after = [i for i, t in enumerate(self.times) if t >= end]
        if not before or not after:
            return []
        return _with_data(self.hub_wind, range(before[-1], after[0] + 1))

    def interval_steps(self, start: datetime, end: datetime) -> list[int]:
        """Interval steps whose (valid - interval, valid] overlaps the window."""
        steps = [
            i
            for i, (t, dt) in enumerate(
                zip(self.times, self.precip_intervals, strict=True)
            )
            if t > start and t - dt < end
        ]
        # The window must be covered: the first interval starts by `start`
        # and the last ends at or after `end`.
        if (
            not steps
            or self.times[steps[0]] - self.precip_intervals[steps[0]] > start
            or self.times[steps[-1]] < end
        ):
            return []
        return _with_data(self.precip, steps)


def _with_data(values: np.ndarray, steps: Any) -> list[int]:
    """Drops steps that are missing for every member."""
    return [i for i in steps if np.isfinite(values[:, i]).any()]


def step_metrics(
    mask: np.ndarray, times: list[datetime], site: Site
) -> dict[str, Any]:
    """Persistence and peak metrics of a (member, step) condition mask."""
    total = int(mask.shape[0])
    all_steps = mask.all(axis=1)
    any_step = mask.any(axis=1)
    per_step = mask.mean(axis=0)
    peak = int(np.argmax(per_step))
    peak_prob = float(per_step[peak])
    return {
        "prob_all_steps": round(float(all_steps.mean()), 3),
        "members_all_steps": int(all_steps.sum()),
        "prob_any_step": round(float(any_step.mean()), 3),
        "members_any_step": int(any_step.sum()),
        "prob_no_steps": round(float((~any_step).mean()), 3),
        "total_members": total,
        "peak_step_prob": round(peak_prob, 3),
        "peak_step_prob_valid_time_local": (
            local_time(times[peak], site).isoformat() if peak_prob else None
        ),
    }


@dataclasses.dataclass
class Window:
    """A candidate maintenance window."""

    site_id: str
    start: datetime
    end: datetime
    probability_safe: float
    method: str
    meets_go_threshold: bool
    details: dict[str, Any]

    def overlaps(
        self, other: "Window", buffer: timedelta = timedelta(0)
    ) -> bool:
        return (
            self.start < other.end + buffer and other.start < self.end + buffer
        )

    def to_dict(self, site: Site | None = None) -> dict[str, Any]:
        out = {
            "site_id": self.site_id,
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "probability_safe": round(self.probability_safe, 3),
            "method": self.method,
            "meets_go_threshold": self.meets_go_threshold,
            **self.details,
        }
        if site is not None:
            out["start_local"] = local_time(self.start, site).isoformat()
            out["end_local"] = local_time(self.end, site).isoformat()
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Window":
        """Rebuilds a window from `to_dict` output (local times are dropped)."""
        data = dict(data)
        for key in ("start_local", "end_local"):
            data.pop(key, None)
        return cls(
            site_id=data.pop("site_id"),
            start=datetime.fromisoformat(data.pop("start_utc")),
            end=datetime.fromisoformat(data.pop("end_utc")),
            probability_safe=data.pop("probability_safe"),
            method=data.pop("method"),
            meets_go_threshold=data.pop("meets_go_threshold"),
            details=data,
        )


class Evaluator:
    """Evaluates one task's limits against one site's ensemble forecast."""

    def __init__(
        self,
        forecast: SiteForecast,
        task: config.TaskLimits,
        gust_factor: float | None = None,
    ):
        self.forecast = forecast
        self.site = forecast.site
        self.task = task
        self.gust_factor = (
            config.gust_factor() if gust_factor is None else gust_factor
        )
        # Hub wind limit that respects both the mean-wind and gust limits.
        self.wind_limit = task.effective_wind_limit(self.gust_factor)
        f = forecast
        self._wind_ok = (f.hub_wind <= self.wind_limit) & np.isfinite(
            f.hub_wind
        )
        limits = task.max_precip_mm_per_h * f.precip_limit_scale
        self._rain_ok = (f.precip <= limits) & np.isfinite(f.precip)
        self.joint_condition = (
            f"hub wind <= {self.wind_limit:.2f} m/s AND precipitation <="
            f" {task.max_precip_mm_per_h} mm/h"
        )

    def _allowed(self, start: datetime, end: datetime) -> bool:
        if not self.task.daylight_only:
            return True
        local_start = local_time(start, self.site)
        local_end = local_time(end, self.site)
        day_open = local_start.replace(
            hour=self.task.work_start_hour, minute=0, second=0, microsecond=0
        )
        day_close = local_start.replace(
            hour=self.task.work_end_hour, minute=0, second=0, microsecond=0
        )
        return day_open <= local_start and local_end <= day_close

    def candidate_starts(
        self, begin: datetime, end: datetime, hours: int
    ) -> list[datetime]:
        """Whole-hour starts of allowed windows inside [begin, end]."""
        times = self.forecast.times
        lo = max(begin, times[0])
        hi = min(end, times[-1])
        t = lo.replace(minute=0, second=0, microsecond=0)
        if t < lo:
            t += _ONE_HOUR
        length = timedelta(hours=hours)
        starts = []
        while t + length <= hi:
            if self._allowed(t, t + length):
                starts.append(t)
            t += _ONE_HOUR
        return starts

    def window(self, start: datetime, hours: int) -> Window | None:
        """Scores [start, start + hours); None if the data cannot cover it."""
        f = self.forecast
        end = start + timedelta(hours=hours)
        wind_steps = f.bracketing_steps(start, end)
        rain_steps = f.interval_steps(start, end)
        if not wind_steps or not rain_steps:
            return None
        wind_ok = self._wind_ok[:, wind_steps]
        rain_ok = self._rain_ok[:, rain_steps]
        safe = wind_ok.all(axis=1) & rain_ok.all(axis=1)
        prob = float(safe.mean())
        wind_times = [f.times[i] for i in wind_steps]
        rain_times = [f.times[i] for i in rain_steps]
        wind_max = np.nanmax(f.hub_wind[:, wind_steps], axis=1)
        rain_max = np.nanmax(
            f.precip[:, rain_steps] / f.precip_limit_scale[rain_steps], axis=1
        )
        details = {
            "lead_hours_at_start": round(
                (start - f.init_time).total_seconds() / 3600
            ),
            "joint_condition": self.joint_condition,
            "members_safe": int(safe.sum()),
            "members_total": f.members,
            "wind_exceedance": {
                "condition": f"hub wind > {self.wind_limit:.2f} m/s",
                **_exceedance(~wind_ok, wind_times, self.site),
            },
            "precipitation_exceedance": {
                "condition": (
                    f"precipitation > {self.task.max_precip_mm_per_h} mm/h"
                ),
                **_exceedance(
                    ~rain_ok,
                    rain_times,
                    self.site,
                    [f.precip_intervals[i] for i in rain_steps],
                ),
            },
            "hub_wind_max_p50_ms": round(percentile(wind_max, 50), 1),
            "hub_wind_max_p90_ms": round(percentile(wind_max, 90), 1),
            "gust_estimate_max_p90_ms": round(
                percentile(wind_max, 90) * self.gust_factor, 1
            ),
            "precip_max_p90_mm_per_h": round(percentile(rain_max, 90), 2),
            "steps": {
                "wind": len(wind_steps),
                "precipitation": len(rain_steps),
            },
            "step_hours": _step_hours(f, wind_steps, rain_steps),
        }
        return Window(
            site_id=self.site.site_id,
            start=start,
            end=end,
            probability_safe=prob,
            method="ensemble_all_steps",
            meets_go_threshold=prob >= self.task.go_probability,
            details=details,
        )


def _exceedance(
    mask: np.ndarray,
    times: list[datetime],
    site: Site,
    intervals: list[timedelta] | None = None,
) -> dict[str, Any]:
    """Exceedance metrics; for interval steps also the peak step's start."""
    m = step_metrics(mask, times, site)
    out = {
        k: m[k]
        for k in (
            "prob_any_step",
            "peak_step_prob",
            "peak_step_prob_valid_time_local",
        )
    }
    if intervals is not None:
        peak = int(np.argmax(mask.mean(axis=0)))
        out["peak_step_interval_start_local"] = (
            local_time(times[peak] - intervals[peak], site).isoformat()
            if m["peak_step_prob"]
            else None
        )
    return out


def _step_hours(
    f: SiteForecast, wind_steps: list[int], rain_steps: list[int]
) -> float:
    gaps = [f.wind_intervals[i] for i in wind_steps] + [
        f.precip_intervals[i] for i in rain_steps
    ]
    return max(g / _ONE_HOUR for g in gaps)


def find_windows(
    evaluator: Evaluator,
    start: datetime,
    end: datetime,
    hours: int | None = None,
    top_k: int = 5,
) -> list[Window]:
    """Best non-overlapping windows, highest probability first.

    Every allowed whole-hour start is scored; windows are then picked
    greedily by probability (earliest first on ties) so that no two overlap.
    """
    hours = hours or evaluator.task.min_hours
    if hours < 1:
        raise ValueError("hours must be >= 1")
    scored = [
        w
        for s in evaluator.candidate_starts(start, end, hours)
        if (w := evaluator.window(s, hours)) is not None
    ]
    scored.sort(key=lambda w: (-w.probability_safe, w.start))
    picked: list[Window] = []
    for w in scored:
        if len(picked) >= top_k:
            break
        if not any(w.overlaps(p) for p in picked):
            picked.append(w)
    return picked


def _group_by_day(
    times: list[datetime], tz: ZoneInfo, interval_ending: bool
) -> dict[date, list[int]]:
    """Step indices per local date; interval steps ending at 00:00 count
    towards the previous day."""
    groups: dict[date, list[int]] = {}
    shift = timedelta(seconds=1) if interval_ending else timedelta(0)
    for i, t in enumerate(times):
        groups.setdefault((t - shift).astimezone(tz).date(), []).append(i)
    return groups


def _covers_day(
    steps: list[int],
    intervals: list[timedelta],
    values: np.ndarray,
    hours: float,
) -> bool:
    covered = sum(
        (intervals[i] for i in steps if np.isfinite(values[:, i]).any()),
        timedelta(0),
    )
    return abs(covered / _ONE_HOUR - hours) < 1e-6


def _stats(values: np.ndarray, prefix: str, unit: str, digits: int) -> dict:
    return {
        f"{prefix}_p{q}_{unit}": round(percentile(values, q), digits)
        for q in SUMMARY_PERCENTILES
    }


def daily_outlook(
    evaluator: Evaluator, begin: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Per-member daily summaries over complete local calendar days.

    Like the WeatherNext ensemble-analysis `--group-by-day --skip-partial`
    output: each member's day is reduced first (maximum hub wind, total
    precipitation), then summarized across members; plus the probability
    that the task's limits are exceeded at any step of the day. A day is
    complete when its steps cover the full local day (23-25 h around DST
    changes). Notes flag the WeatherNext daily-summary caveats: maxima of
    steps coarser than 1 h understate the true peak, and coarse rain totals
    are only given when the step intervals line up with local midnight.
    """
    f = evaluator.forecast
    tz = ZoneInfo(f.site.timezone)
    wind_days = _group_by_day(f.times, tz, interval_ending=False)
    rain_days = _group_by_day(f.times, tz, interval_ending=True)
    out = []
    for day in sorted(set(wind_days) & set(rain_days)):
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        if day_end <= begin or day_end > end:
            continue  # outside, or running past, the planning horizon
        hours = expected_hours(day, tz)
        ws, rs = wind_days[day], rain_days[day]
        if not (
            _covers_day(ws, f.wind_intervals, f.hub_wind, hours)
            and _covers_day(rs, f.precip_intervals, f.precip, hours)
        ):
            continue
        notes = []
        wind_step = max(f.wind_intervals[i] for i in ws) / _ONE_HOUR
        rain_step = max(f.precip_intervals[i] for i in rs) / _ONE_HOUR
        member_max = np.nanmax(f.hub_wind[:, ws], axis=1)
        row: dict[str, Any] = {
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "complete": True,
            "step_hours": max(wind_step, rain_step),
            **_stats(member_max, "hub_wind_max", "ms", 1),
            "gust_estimate_max_p90_ms": round(
                percentile(member_max, 90) * evaluator.gust_factor, 1
            ),
            "prob_any_step_wind_over_limit": round(
                float((~evaluator._wind_ok[:, ws]).any(axis=1).mean()), 3
            ),
            "prob_any_step_precip_over_limit": round(
                float((~evaluator._rain_ok[:, rs]).any(axis=1).mean()), 3
            ),
        }
        if wind_step > 1:
            notes.append(
                f"hub wind maximum is over {wind_step:g}-hourly samples and"
                " understates the true daily peak"
            )
        first = rs[0]
        aligned = f.times[first] - f.precip_intervals[first] == day_start
        if rain_step > 1 and not aligned:
            notes.append(
                f"no daily rain total: {rain_step:g}-hour intervals do not"
                " line up with local midnight"
            )
        else:
            totals = np.nansum(
                f.precip[:, rs]
                / f.precip_limit_scale[rs]
                * np.array([f.precip_intervals[i] / _ONE_HOUR for i in rs]),
                axis=1,
            )
            row.update(_stats(totals, "precip_total", "mm", 2))
        if notes:
            row["notes"] = notes
        out.append(row)
    return out


def plan_crews(
    windows_by_site: dict[str, list[Window]],
    crews: int,
    travel_hours: float = 12.0,
    min_probability: float = 0.0,
) -> dict[str, Any]:
    """Assigns crews to windows across sites, one job per site.

    Greedy: candidate windows are taken highest probability first (earliest on
    ties); each goes to the first crew whose existing jobs are at least
    `travel_hours` away, if its site has no job yet.

    Returns:
        `assignments` (Window, crew number), `unassigned` (site_id -> reason)
        and `expected_completed_jobs` (sum of window probabilities).
    """
    if crews < 1:
        raise ValueError("crews must be >= 1")
    buffer = timedelta(hours=travel_hours)
    candidates = [
        w
        for ws in windows_by_site.values()
        for w in ws
        if w.probability_safe >= min_probability
    ]
    candidates.sort(key=lambda w: (-w.probability_safe, w.start, w.site_id))
    schedule: dict[int, list[Window]] = {c: [] for c in range(1, crews + 1)}
    assigned: dict[str, tuple[Window, int]] = {}
    for w in candidates:
        if w.site_id in assigned:
            continue
        for crew, jobs in schedule.items():
            if not any(w.overlaps(j, buffer) for j in jobs):
                jobs.append(w)
                assigned[w.site_id] = (w, crew)
                break
    unassigned = {}
    for site_id, ws in windows_by_site.items():
        if site_id in assigned:
            continue
        best = max(ws, key=lambda w: w.probability_safe, default=None)
        if best is None:
            unassigned[site_id] = (
                "no allowed window of the required length in the horizon"
            )
        elif best.probability_safe < min_probability:
            unassigned[site_id] = (
                f"best window has P(safe)="
                f"{best.probability_safe:.2f} < required {min_probability:.2f}"
            )
        else:
            unassigned[site_id] = "no crew free for any qualifying window"
    assignments = sorted(
        ((w, c) for w, c in assigned.values()), key=lambda x: (x[1], x[0].start)
    )
    return {
        "assignments": assignments,
        "unassigned": unassigned,
        "expected_completed_jobs": round(
            sum(w.probability_safe for w, _ in assignments), 2
        ),
    }
