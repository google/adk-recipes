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
"""Unit tests for the forecast math, window finding and crew planning."""

from datetime import UTC, datetime, timedelta

import pytest

from app import analysis, config
from app.forecast import PRECIP, WIND, Forecast, Site

INIT = datetime(2026, 10, 12, 0, tzinfo=UTC)
SITE = Site("s1", "Site 1", 0.0, 0.0, utc_offset_hours=0, hub_height_m=100)
TASK = config.TaskLimits(
    description="test",
    max_wind_ms=10.0,
    max_gust_ms=100.0,  # gust limit not binding unless a test says so
    max_precip_mm=0.5,
    min_hours=3,
    daylight_only=False,
    go_probability=0.75,
)


def member_forecast(winds, precs=None, site_id="s1", start_hour=1, times=None):
    n = len(winds[0])
    precs = precs or [[0.0] * n for _ in winds]
    times = times or [INIT + timedelta(hours=start_hour + h) for h in range(n)]
    return Forecast(
        site_id, INIT, times, "test", members={WIND: winds, PRECIP: precs}
    )


def evaluator(forecast, site=SITE, task=TASK, gust_factor=1.0, shear=0.0):
    return analysis.Evaluator(forecast, site, task, gust_factor, shear)


# --- percentile / quantile math -------------------------------------------


def test_percentile_matches_linear_interpolation():
    values = [4.0, 1.0, 3.0, 2.0]
    assert analysis.percentile(values, 0) == 1.0
    assert analysis.percentile(values, 100) == 4.0
    assert analysis.percentile(values, 50) == 2.5
    assert analysis.percentile(values, 90) == pytest.approx(3.7)


def test_percentile_rejects_bad_input():
    with pytest.raises(ValueError):
        analysis.percentile([], 50)
    with pytest.raises(ValueError):
        analysis.percentile([1.0], 101)


QS = {0.1: 4.0, 0.25: 6.0, 0.5: 8.0, 0.75: 10.0, 0.9: 13.0}


def test_quantile_cdf_interior_and_exact_points():
    assert analysis.quantile_cdf(8.0, QS) == pytest.approx(0.5)
    assert analysis.quantile_cdf(9.0, QS) == pytest.approx(0.625)
    assert analysis.quantile_cdf(11.5, QS) == pytest.approx(0.825)


def test_quantile_cdf_tails_are_extrapolated_and_clamped():
    # Below p10, the p10-p25 slope (0.075 per m/s) is extended.
    assert analysis.quantile_cdf(3.0, QS) == pytest.approx(0.025)
    assert analysis.quantile_cdf(-100.0, QS) == 0.0
    # Above p90, the p75-p90 slope (0.05 per m/s) is extended.
    assert analysis.quantile_cdf(14.0, QS) == pytest.approx(0.95)
    assert analysis.quantile_cdf(100.0, QS) == 1.0


def test_quantile_cdf_handles_repeated_zero_precipitation():
    precip = {0.1: 0.0, 0.25: 0.0, 0.5: 0.0, 0.75: 0.2, 0.9: 1.0}
    assert analysis.quantile_cdf(0.0, precip) == pytest.approx(0.5)
    assert analysis.quantile_cdf(0.1, precip) == pytest.approx(0.625)
    all_dry = dict.fromkeys(QS, 0.0)
    assert analysis.quantile_cdf(0.5, all_dry) == 1.0


def test_hub_scale_power_law():
    tall = Site("t", "t", 0, 0, 0, hub_height_m=120)
    assert analysis.hub_scale(tall, 0.14) == pytest.approx((1.2) ** 0.14)
    assert analysis.hub_scale(SITE, 0.14) == pytest.approx(1.0)


def test_effective_wind_limit_uses_gust_limit_when_tighter():
    task = config.DEFAULT_TASKS["main_component_lift"]
    assert task.effective_wind_limit(1.0) == 10.0
    assert task.effective_wind_limit(1.5) == pytest.approx(14.0 / 1.5)


# --- window probability ---------------------------------------------------


def test_member_window_probability_is_joint_across_hours():
    winds = [
        [5, 5, 5, 5],
        [5, 5, 5, 5],
        [5, 11, 5, 5],  # one gusty hour breaks the whole window
        [5, 5, 5, 11],  # outside a 3-hour window starting at hour 0
    ]
    w = evaluator(member_forecast(winds)).window(0, 3)
    assert w.probability_safe == 0.75
    assert w.details["members_safe"] == 3
    assert w.details["fail_fraction_wind_or_gust"] == 0.25
    assert w.meets_go_threshold
    assert w.end - w.start == timedelta(hours=3)


def test_member_window_counts_precipitation_failures():
    winds = [[5, 5, 5]] * 4
    precs = [[0, 0, 0], [0, 2.0, 0], [0, 0, 0.6], [0, 0, 0.5]]
    w = evaluator(member_forecast(winds, precs)).window(0, 3)
    assert w.probability_safe == 0.5
    assert w.details["fail_fraction_precipitation"] == 0.5
    assert not w.meets_go_threshold


def test_threshold_is_inclusive():
    w = evaluator(member_forecast([[10.0, 10.0, 10.0]])).window(0, 3)
    assert w.probability_safe == 1.0


def test_gust_factor_and_shear_tighten_limit():
    winds = [[8.0] * 3]
    gusty = config.TaskLimits("t", 10.0, 10.0, 0.5, 3, daylight_only=False)
    # Gust 8 * 1.35 = 10.8 > 10 -> unsafe.
    assert evaluator(
        member_forecast(winds), task=gusty, gust_factor=1.35
    ).window(0, 3).probability_safe == pytest.approx(0.0)
    # Taller hub: 9.6 m/s at 100 m becomes > 10 m/s at 150 m with alpha 0.14.
    tall = Site("s1", "t", 0, 0, 0, hub_height_m=150)
    ev = evaluator(member_forecast([[9.6] * 3]), site=tall, shear=0.14)
    assert ev.window(0, 3).probability_safe == 0.0


def test_quantile_window_reports_estimate_and_lower_bound():
    times = [INIT + timedelta(hours=h) for h in (1, 2)]
    wind = {
        lv: [v, v] for lv, v in QS.items()
    }  # P(wind <= 10) = 0.75 each hour
    dry = {lv: [0.0, 0.0] for lv in QS}
    f = Forecast("s1", INIT, times, "test", quantiles={WIND: wind, PRECIP: dry})
    w = evaluator(f).window(0, 2)
    assert w.method == "quantile_marginals"
    assert w.probability_safe == pytest.approx(0.75)
    assert w.details["probability_safe_lower_bound"] == pytest.approx(0.5)


# --- window search --------------------------------------------------------


def test_find_windows_ranks_and_avoids_overlap():
    #       index: 0  1  2  3  4   5  6   7  8
    calm = [5, 5, 5, 5, 12, 5, 5, 5, 5]
    windy_late = [5, 5, 5, 5, 12, 5, 12, 5, 5]
    ev = evaluator(member_forecast([calm, windy_late]))
    windows = analysis.find_windows(ev, hours=3, top_k=5)
    # Starts 0 and 1 are safe for both members (earliest wins the tie); 5 and 6
    # only for `calm`; every other start overlaps an already picked window.
    assert [(w.start - INIT).total_seconds() / 3600 - 1 for w in windows] == [
        0,
        5,
    ]
    assert [w.probability_safe for w in windows] == [1.0, 0.5]
    assert not windows[0].overlaps(windows[1])


def test_find_windows_top_k_and_too_short_horizon():
    ev = evaluator(member_forecast([[5] * 10]))
    assert len(analysis.find_windows(ev, hours=2, top_k=2)) == 2
    assert analysis.find_windows(ev, hours=11) == []
    assert len(analysis.find_windows(ev, hours=10)) == 1
    with pytest.raises(ValueError):
        analysis.find_windows(ev, hours=-1)


def test_find_windows_respects_start_and_end():
    ev = evaluator(member_forecast([[5] * 12]))
    start = INIT + timedelta(hours=4)
    end = INIT + timedelta(hours=8)
    windows = analysis.find_windows(ev, start, end, hours=2, top_k=10)
    assert windows
    assert all(w.start >= start and w.end <= end for w in windows)


def test_find_windows_skips_gaps_in_data():
    times = [INIT + timedelta(hours=h) for h in (1, 2, 3, 5, 6, 7)]
    ev = evaluator(member_forecast([[5] * 6], times=times))
    windows = analysis.find_windows(ev, hours=3, top_k=10)
    assert [w.start.hour for w in windows] == [1, 5]


def test_daylight_only_uses_site_local_time():
    site = Site("s1", "t", 0, 0, utc_offset_hours=-5, hub_height_m=100)
    task = config.TaskLimits(
        "t", 10, 100, 0.5, 2, work_start_hour=7, work_end_hour=10
    )
    f = member_forecast([[5] * 30], start_hour=0)
    windows = analysis.find_windows(
        evaluator(f, site=site, task=task), top_k=10
    )
    # Local 07:00-10:00 is 12:00-15:00 UTC: 2-hour windows at 12:00 only (non-overlap).
    assert [w.start.hour for w in windows] == [12]
    assert (
        windows[0]
        .to_dict(site)["start_local"]
        .startswith("2026-10-12T07:00:00-05:00")
    )


# --- summaries ------------------------------------------------------------


def test_summarize_members_block_percentiles():
    winds = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]
    precs = [[0, 1, 0, 0], [0, 0, 0, 0], [1, 1, 1, 1]]
    blocks = analysis.summarize(
        member_forecast(winds, precs),
        SITE,
        block_hours=2,
        gust_factor=1.5,
        shear_exponent=0,
    )
    assert len(blocks) == 2
    assert blocks[0]["hub_wind_max_p50_ms"] == 6
    assert blocks[0]["hub_wind_max_p90_ms"] == pytest.approx(9.2)
    assert blocks[0]["gust_max_p90_ms"] == pytest.approx(13.8)
    assert blocks[0]["precip_total_p50_mm"] == 1.0
    assert blocks[1]["hub_wind_max_p10_ms"] == pytest.approx(4.8)


# --- crew planning --------------------------------------------------------


def make_window(site_id, start_hour, prob, hours=8):
    start = INIT + timedelta(hours=start_hour)
    return analysis.Window(
        site_id,
        start,
        start + timedelta(hours=hours),
        prob,
        "ensemble_members",
        True,
        {},
    )


def test_plan_crews_ranks_by_probability_one_job_per_site():
    windows = {
        "a": [make_window("a", 10, 0.9), make_window("a", 40, 0.95)],
        "b": [make_window("b", 10, 0.85)],
        "c": [make_window("c", 60, 0.5)],
    }
    plan = analysis.plan_crews(
        windows, crews=2, travel_hours=12, min_probability=0.8
    )
    got = {w.site_id: (w.start, c) for w, c in plan["assignments"]}
    assert got["a"][0] == INIT + timedelta(hours=40)  # best window for site a
    assert set(got) == {"a", "b"}
    assert "0.50 < required 0.80" in plan["unassigned"]["c"]
    assert plan["expected_completed_jobs"] == pytest.approx(1.8)


def test_plan_crews_respects_travel_buffer():
    windows = {
        "a": [make_window("a", 10, 0.9)],
        "b": [make_window("b", 22, 0.9)],  # starts 4 h after a ends
    }
    one_crew = analysis.plan_crews(windows, crews=1, travel_hours=6)
    assert len(one_crew["assignments"]) == 1
    assert (
        one_crew["unassigned"]["b"] == "no crew free for any qualifying window"
    )
    two_crews = analysis.plan_crews(windows, crews=2, travel_hours=6)
    assert sorted(c for _, c in two_crews["assignments"]) == [1, 2]
    short_travel = analysis.plan_crews(windows, crews=1, travel_hours=2)
    assert len(short_travel["assignments"]) == 2


def test_plan_crews_falls_back_to_next_window_for_busy_crew():
    windows = {
        "a": [make_window("a", 10, 0.95)],
        "b": [make_window("b", 12, 0.9), make_window("b", 40, 0.8)],
    }
    plan = analysis.plan_crews(windows, crews=1, travel_hours=6)
    got = {w.site_id: w.start for w, _ in plan["assignments"]}
    assert got["b"] == INIT + timedelta(hours=40)


def test_plan_crews_validation_and_empty_sites():
    with pytest.raises(ValueError):
        analysis.plan_crews({}, crews=0)
    plan = analysis.plan_crews({"a": []}, crews=1)
    assert "no allowed window" in plan["unassigned"]["a"]
