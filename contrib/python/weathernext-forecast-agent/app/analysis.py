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
"""Deterministic forecast math: percentiles, window probabilities, crew plans.

No model is involved here. The agent only calls these functions through tools
and explains their results.

Probability of a safe window
----------------------------
* Member-level ensembles (the bundled sample, or WeatherNext's raw 64-member
  Zarr output): a window is safe for a member when every hour in it is within
  the task limits. P(safe) is the fraction of members that are safe for the
  whole window, so the hour-to-hour and wind-to-rain correlations of each
  member are kept.
* Quantile statistics (WeatherNext on BigQuery): only per-hour marginal
  distributions are known. The recipe reports two numbers:
  `probability_safe` = min over hours and variables of the marginal P(safe)
  (exact if conditions are perfectly correlated, otherwise optimistic), and
  `probability_safe_lower_bound` = max(0, 1 - sum of marginal P(unsafe))
  (the Frechet lower bound, valid for any correlation).
"""

import dataclasses
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config
from .forecast import PRECIP, WIND, Forecast, Site

# Percentiles reported by `summarize`.
SUMMARY_PERCENTILES = (10, 50, 90)


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (same as numpy's default), q in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty list")
    if not 0 <= q <= 100:
        raise ValueError(f"q must be in [0, 100], got {q}")
    data = sorted(values)
    pos = (len(data) - 1) * q / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def quantile_cdf(threshold: float, quantiles: dict[float, float]) -> float:
    """Estimates P(X <= threshold) from a few quantiles of X.

    Linear between known quantiles; beyond the outermost ones, the slope of the
    nearest segment is extended and the result clamped to [0, 1]. Repeated
    quantile values (for example many zero-precipitation quantiles) are
    handled by taking the highest level whose value is <= threshold.
    """
    pts = sorted(quantiles.items())
    levels = [p[0] for p in pts]
    vals = [p[1] for p in pts]
    if threshold >= vals[-1]:
        if len(pts) < 2 or vals[-1] == vals[-2]:
            return 1.0
        slope = (levels[-1] - levels[-2]) / (vals[-1] - vals[-2])
        return min(1.0, levels[-1] + slope * (threshold - vals[-1]))
    if threshold < vals[0]:
        if len(pts) < 2 or vals[1] == vals[0]:
            return 0.0
        slope = (levels[1] - levels[0]) / (vals[1] - vals[0])
        return max(0.0, levels[0] - slope * (vals[0] - threshold))
    # vals[0] <= threshold < vals[-1]: find the last i with vals[i] <= threshold.
    i = max(k for k, v in enumerate(vals) if v <= threshold)
    if vals[i + 1] == vals[i]:
        return levels[i]
    frac = (threshold - vals[i]) / (vals[i + 1] - vals[i])
    return levels[i] + frac * (levels[i + 1] - levels[i])


def hub_scale(site: Site, shear_exponent: float | None = None) -> float:
    """Factor converting 100 m wind speed to the site's hub height."""
    alpha = (
        config.shear_exponent() if shear_exponent is None else shear_exponent
    )
    return (site.hub_height_m / config.REFERENCE_HEIGHT_M) ** alpha


def local_time(t: datetime, site: Site) -> datetime:
    return t.astimezone(timezone(timedelta(hours=site.utc_offset_hours)))


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


class Evaluator:
    """Evaluates task limits against one site's forecast."""

    def __init__(
        self,
        forecast: Forecast,
        site: Site,
        task: config.TaskLimits,
        gust_factor: float | None = None,
        shear_exponent: float | None = None,
    ):
        self.forecast = forecast
        self.site = site
        self.task = task
        self.gust_factor = (
            config.gust_factor() if gust_factor is None else gust_factor
        )
        self.scale = hub_scale(site, shear_exponent)
        # Limit on the 100 m wind that respects both hub wind and gust limits.
        self.wind_limit_100m = (
            task.effective_wind_limit(self.gust_factor) / self.scale
        )
        if forecast.kind == "members":
            self._wind_ok = [
                [w <= self.wind_limit_100m for w in row]
                for row in forecast.members[WIND]
            ]
            self._rain_ok = [
                [p <= task.max_precip_mm for p in row]
                for row in forecast.members[PRECIP]
            ]
        else:
            q = forecast.quantiles
            n = len(forecast.times)
            self._p_wind = [
                quantile_cdf(
                    self.wind_limit_100m,
                    {lv: s[h] for lv, s in q[WIND].items()},
                )
                for h in range(n)
            ]
            self._p_rain = [
                quantile_cdf(
                    task.max_precip_mm,
                    {lv: s[h] for lv, s in q[PRECIP].items()},
                )
                for h in range(n)
            ]

    def _allowed_hour(self, t: datetime) -> bool:
        if not self.task.daylight_only:
            return True
        hour = local_time(t, self.site).hour
        return self.task.work_start_hour <= hour < self.task.work_end_hour

    def candidate_starts(self, lo: int, hi: int, hours: int) -> list[int]:
        """Start indices of contiguous, allowed windows inside [lo, hi)."""
        times = self.forecast.times
        starts = []
        for i in range(lo, hi - hours + 1):
            if times[i + hours - 1] - times[i] != timedelta(hours=hours - 1):
                continue  # gap in the data
            if all(self._allowed_hour(times[k]) for k in range(i, i + hours)):
                starts.append(i)
        return starts

    def window(self, i: int, hours: int) -> Window:
        """Probability that hours [i, i + hours) are all within limits."""
        f = self.forecast
        span = range(i, i + hours)
        if f.kind == "members":
            n = len(self._wind_ok)
            wind_fail = [not all(row[k] for k in span) for row in self._wind_ok]
            rain_fail = [not all(row[k] for k in span) for row in self._rain_ok]
            safe = sum(
                1
                for w, r in zip(wind_fail, rain_fail, strict=True)
                if not (w or r)
            )
            prob = safe / n
            wmax = [
                max(row[k] for k in span) * self.scale
                for row in f.members[WIND]
            ]
            pmax = [max(row[k] for k in span) for row in f.members[PRECIP]]
            details = {
                "members_safe": safe,
                "members_total": n,
                "fail_fraction_wind_or_gust": round(sum(wind_fail) / n, 3),
                "fail_fraction_precipitation": round(sum(rain_fail) / n, 3),
                "hub_wind_max_p50_ms": round(percentile(wmax, 50), 1),
                "hub_wind_max_p90_ms": round(percentile(wmax, 90), 1),
                "gust_max_p90_ms": round(
                    percentile(wmax, 90) * self.gust_factor, 1
                ),
                "precip_max_p90_mm": round(percentile(pmax, 90), 2),
            }
            method = "ensemble_members"
        else:
            marg = [self._p_wind[k] for k in span] + [
                self._p_rain[k] for k in span
            ]
            prob = min(marg)
            lower = max(0.0, 1.0 - sum(1.0 - p for p in marg))
            q = f.quantiles
            details = {
                "probability_safe_lower_bound": round(lower, 3),
                "min_hourly_p_wind_ok": round(
                    min(self._p_wind[k] for k in span), 3
                ),
                "min_hourly_p_precip_ok": round(
                    min(self._p_rain[k] for k in span), 3
                ),
                "hub_wind_max_p50_ms": round(
                    max(q[WIND][0.5][k] for k in span) * self.scale, 1
                ),
                "hub_wind_max_p90_ms": round(
                    max(q[WIND][0.9][k] for k in span) * self.scale, 1
                ),
                "gust_max_p90_ms": round(
                    max(q[WIND][0.9][k] for k in span)
                    * self.scale
                    * self.gust_factor,
                    1,
                ),
                "precip_max_p90_mm": round(
                    max(q[PRECIP][0.9][k] for k in span), 2
                ),
            }
            method = "quantile_marginals"
        return Window(
            site_id=self.site.site_id,
            start=f.times[i],
            end=f.times[i + hours - 1] + timedelta(hours=1),
            probability_safe=prob,
            method=method,
            meets_go_threshold=prob >= self.task.go_probability,
            details={
                "lead_hours_at_start": round(
                    (f.times[i] - f.init_time).total_seconds() / 3600
                ),
                **details,
            },
        )


def find_windows(
    evaluator: Evaluator,
    start: datetime | None = None,
    end: datetime | None = None,
    hours: int | None = None,
    top_k: int = 5,
) -> list[Window]:
    """Best non-overlapping windows, highest probability first.

    Every allowed start hour is scored; windows are then picked greedily by
    probability (earliest first on ties) so that no two overlap.
    """
    hours = hours or evaluator.task.min_hours
    if hours < 1:
        raise ValueError("hours must be >= 1")
    lo, hi = evaluator.forecast.index_range(start, end)
    scored = [
        evaluator.window(i, hours)
        for i in evaluator.candidate_starts(lo, hi, hours)
    ]
    scored.sort(key=lambda w: (-w.probability_safe, w.start))
    picked: list[Window] = []
    for w in scored:
        if len(picked) >= top_k:
            break
        if not any(w.overlaps(p) for p in picked):
            picked.append(w)
    return picked


def summarize(
    forecast: Forecast,
    site: Site,
    start: datetime | None = None,
    end: datetime | None = None,
    block_hours: int = 6,
    gust_factor: float | None = None,
    shear_exponent: float | None = None,
) -> list[dict[str, Any]]:
    """Ensemble percentiles per block of `block_hours` hours.

    For member ensembles each member's block maximum hub wind and block total
    precipitation are computed first, then percentiles are taken across
    members. For quantile statistics the block maximum (wind) and block sum
    (precipitation) of each quantile series are reported, which is an
    approximation.
    """
    if block_hours < 1:
        raise ValueError("block_hours must be >= 1")
    gf = config.gust_factor() if gust_factor is None else gust_factor
    scale = hub_scale(site, shear_exponent)
    lo, hi = forecast.index_range(start, end)
    blocks = []
    for b in range(lo, hi, block_hours):
        span = range(b, min(b + block_hours, hi))
        row: dict[str, Any] = {
            "start_utc": forecast.times[span[0]].isoformat(),
            "start_local": local_time(
                forecast.times[span[0]], site
            ).isoformat(),
            "hours": len(span),
        }
        if forecast.kind == "members":
            wmax = [
                max(m[k] for k in span) * scale for m in forecast.members[WIND]
            ]
            ptot = [sum(m[k] for k in span) for m in forecast.members[PRECIP]]
            stats = {
                "hub_wind_max": {
                    q: percentile(wmax, q) for q in SUMMARY_PERCENTILES
                },
                "precip_total": {
                    q: percentile(ptot, q) for q in SUMMARY_PERCENTILES
                },
            }
        else:
            qs = forecast.quantiles
            stats = {
                "hub_wind_max": {
                    q: max(qs[WIND][q / 100][k] for k in span) * scale
                    for q in SUMMARY_PERCENTILES
                },
                "precip_total": {
                    q: sum(qs[PRECIP][q / 100][k] for k in span)
                    for q in SUMMARY_PERCENTILES
                },
            }
        for q in SUMMARY_PERCENTILES:
            row[f"hub_wind_max_p{q}_ms"] = round(stats["hub_wind_max"][q], 1)
        row["gust_max_p90_ms"] = round(stats["hub_wind_max"][90] * gf, 1)
        for q in SUMMARY_PERCENTILES:
            row[f"precip_total_p{q}_mm"] = round(stats["precip_total"][q], 2)
        blocks.append(row)
    return blocks


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
                f"best window {best.start.isoformat()} has P(safe)="
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
