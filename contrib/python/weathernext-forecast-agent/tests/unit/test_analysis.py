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
"""Unit tests for the dataset contract, ensemble probabilities and crews."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from synthetic import INIT, make_ds

from app import analysis, config, contract
from app.forecast import Site

SITE = Site("s1", "Site 1", 55.7, -4.25, timezone="UTC", hub_height_m=100)
TASK = config.TaskLimits(
    description="test",
    max_wind_ms=10.0,
    max_gust_ms=100.0,  # gust limit not binding unless a test says so
    max_precip_mm_per_h=0.5,
    min_hours=3,
    daylight_only=False,
    go_probability=0.75,
)


def evaluator(ds, site=SITE, task=TASK, gust_factor=1.0, shear=0.0):
    forecast = analysis.SiteForecast.from_dataset(ds, site, shear)
    return analysis.Evaluator(forecast, task, gust_factor)


def at(hours):
    return INIT + timedelta(hours=hours)


# --- contract: metadata and units -----------------------------------------


def test_units_follow_cf_metadata():
    ds = make_ds([[10.0], [20.0]], [[1.8], [3.6]], wind_units="knots")
    wind = contract.to_units(ds["ws"], "m/s")
    assert wind.values[:, 0] == pytest.approx([5.14444, 10.28888])
    assert wind.attrs["units"] == "m/s"
    rate = contract.to_units(ds["pr"], "mm/h")  # kg m-2 s-1 -> mm/h
    assert rate.values[:, 0] == pytest.approx([1.8, 3.6])
    amount = make_ds([[1.0]], [[2.0]], precip_kind="amount")["pr"]
    assert contract.to_units(amount, "mm").values[0, 0] == pytest.approx(2.0)
    # m s**-1 (WeatherNext spelling) is the same unit as m s-1.
    ds["ws"].attrs["units"] = "m s**-1"
    assert contract.to_units(ds["ws"], "m/s").values[0, 0] == 10.0


def test_units_never_turn_an_accumulation_into_a_rate():
    amount = make_ds([[1.0]], [[2.0]], precip_kind="amount")["pr"]
    with pytest.raises(contract.ContractError, match="cannot be expressed"):
        contract.to_units(amount, "mm/h")
    ds = make_ds([[1.0]])
    ds["ws"].attrs["units"] = "furlongs/fortnight"
    with pytest.raises(contract.ContractError, match="cannot convert"):
        contract.to_units(ds["ws"], "m/s")


def test_variables_are_found_by_standard_name_not_name():
    ds = make_ds(
        [[5.0, 5.0], [5.0, 12.0]],
        wind_name="anything_at_all",
        precip_name="whatever",
        member_dim="sample",
    )
    ev = evaluator(ds)
    assert ev.window(at(1), 1).probability_safe == 0.5
    assert contract.realization_dim(ds) == "sample"
    assert contract.point_location(ds) == (55.7, -4.25)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda ds: ds["ws"].attrs.update(statistic="p90"), "reduced"),
        (lambda ds: ds["ws"].attrs.pop("units"), "units"),
        (lambda ds: ds["pr"].attrs.update(step_type="sum"), "step_type"),
        (
            lambda ds: ds["number"].attrs.update(standard_name="member"),
            "realization",
        ),
    ],
)
def test_contract_violations_are_rejected(change, message):
    ds = make_ds([[1.0, 1.0], [1.0, 1.0]])
    change(ds)
    with pytest.raises(contract.ContractError, match=message):
        contract.validate(ds)


def test_contract_needs_more_than_one_member_and_a_step_period():
    with pytest.raises(contract.ContractError, match="more than one"):
        contract.validate(make_ds([[1.0, 2.0]]))
    ds = make_ds([[1.0], [1.0]], precip_kind="amount")
    del ds["pr"].attrs["step_period"]
    with pytest.raises(contract.ContractError, match="step_period"):
        contract.validate(ds)


def test_step_intervals_follow_step_type():
    ds = make_ds([[1.0] * 3] * 2, lead_hours=[0, 6, 18])
    hours = [
        i / timedelta(hours=1) for i in contract.step_intervals(ds, ds["ws"])
    ]
    assert hours == [6, 12, 12]  # instant: gap to the next step
    hours = [
        i / timedelta(hours=1) for i in contract.step_intervals(ds, ds["pr"])
    ]
    assert hours == [6, 6, 12]  # avg: gap to the previous step
    acc = make_ds([[1.0] * 3] * 2, lead_hours=[0, 6, 18], precip_kind="amount")
    assert set(contract.step_intervals(acc, acc["pr"])) == {timedelta(hours=1)}


def test_wind_needs_its_height_for_hub_extrapolation():
    ds = make_ds([[1.0], [1.0]])
    del ds["ws"].attrs["height_above_ground_m"]
    with pytest.raises(contract.ContractError, match="height_above_ground_m"):
        analysis.SiteForecast.from_dataset(ds, SITE, 0.14)


# --- probabilities ---------------------------------------------------------


def test_window_probability_is_all_steps_persistence_per_member():
    winds = [
        [5, 5, 5, 5],
        [5, 11, 5, 5],  # one bad step ruins the whole window
        [5, 5, 5, 5],
        [5, 5, 5, 5],
    ]
    w = evaluator(make_ds(winds)).window(at(1), 3)
    assert w.probability_safe == 0.75
    assert (w.details["members_safe"], w.details["members_total"]) == (3, 4)
    assert w.method == "ensemble_all_steps"
    assert w.meets_go_threshold
    exc = w.details["wind_exceedance"]
    assert exc["prob_any_step"] == 0.25
    assert exc["peak_step_prob"] == 0.25
    assert exc["peak_step_prob_valid_time_local"] == at(2).isoformat()


def test_joint_condition_counts_each_member_once():
    winds = [[5, 11], [5, 5], [5, 5], [5, 5]]
    precs = [[0, 2.0], [0, 0], [0, 2.0], [0, 0]]  # member 0 fails both
    w = evaluator(make_ds(winds, precs)).window(at(1), 1)
    assert w.probability_safe == 0.5
    assert w.details["precipitation_exceedance"]["prob_any_step"] == 0.5
    assert w.details["wind_exceedance"]["prob_any_step"] == 0.25


def test_limits_are_inclusive_and_gust_and_shear_tighten_them():
    ds = make_ds([[10.0, 10.0], [10.0, 10.0]])
    assert evaluator(ds).window(at(1), 1).probability_safe == 1.0
    tight = config.TaskLimits(
        description="t",
        max_wind_ms=10.0,
        max_gust_ms=12.0,
        max_precip_mm_per_h=0.5,
        min_hours=1,
        daylight_only=False,
    )
    # gust limit / 1.35 = 8.9 m/s < 10 m/s.
    assert evaluator(ds, task=tight, gust_factor=1.35).wind_limit == (
        pytest.approx(12 / 1.35)
    )
    assert (
        evaluator(ds, task=tight, gust_factor=1.35)
        .window(at(1), 1)
        .probability_safe
        == 0.0
    )
    # 9.8 m/s at 100 m is 10.1 m/s at a 125 m hub with alpha = 0.14.
    high_hub = Site("s2", "S2", 0, 0, timezone="UTC", hub_height_m=125)
    ds = make_ds([[9.8, 9.8], [9.8, 9.8]])
    assert (
        evaluator(ds, site=high_hub, shear=0.14)
        .window(at(1), 1)
        .probability_safe
        == 0.0
    )
    assert analysis.hub_scale(high_hub, 100, 0.14) == pytest.approx(1.25**0.14)


def test_missing_values_never_satisfy_and_all_missing_steps_are_skipped():
    nan = float("nan")
    winds = [[5, nan, 5], [5, 5, 5]]
    w = evaluator(make_ds(winds)).window(at(1), 2)
    assert w.probability_safe == 0.5  # member 0 has a NaN step
    winds = [[5, nan, 5], [5, nan, 5]]
    w = evaluator(make_ds(winds)).window(at(1), 2)
    assert w.probability_safe == 1.0
    assert w.details["steps"]["wind"] == 2


def test_accumulation_limit_scales_with_step_period():
    # 1-hour accumulations in metres: 0.4 mm and 0.6 mm against 0.5 mm/h.
    precs = [[0.0, 0.4], [0.0, 0.6]]
    ds = make_ds([[1, 1], [1, 1]], precs, precip_kind="amount")
    w = evaluator(ds).window(at(1), 1)
    assert w.probability_safe == 0.5
    assert w.details["precip_max_p90_mm_per_h"] == pytest.approx(0.58)


def test_six_hourly_steps_bracket_instant_and_overlap_intervals():
    # Steps every 6 h from 00 UTC: an 07-15 window brackets wind at 06, 12
    # and 18 and uses the rain intervals ending 12 (06-12) and 18 (12-18).
    winds = [[20, 20, 5, 5, 5, 20], [20, 20, 5, 5, 11, 20]]
    precs = [[0, 0, 0, 0, 0, 9], [0, 0, 0, 0, 0, 9]]
    ds = make_ds(winds, precs, lead_hours=[0, 6, 12, 18, 24, 30])
    ev = evaluator(ds, gust_factor=1.0)
    start = INIT + timedelta(hours=7)
    assert ev.forecast.bracketing_steps(start, start + timedelta(hours=8)) == [
        1,
        2,
        3,
    ]
    assert ev.forecast.interval_steps(start, start + timedelta(hours=8)) == [
        2,
        3,
    ]
    w = ev.window(start, 8)
    assert w is None or w.probability_safe == 0.0  # 20 m/s at 06 UTC
    start = INIT + timedelta(hours=12)
    w = ev.window(start, 6)  # 12-18: wind at 12, 18; rain 12-18
    assert w.probability_safe == 1.0
    assert w.details["step_hours"] == 6


def test_find_windows_ranks_avoids_overlap_and_respects_bounds():
    winds = [[5] * 8 + [20] * 4 + [5] * 6, [5] * 8 + [20] * 4 + [11] * 6]
    ev = evaluator(make_ds(winds))
    windows = analysis.find_windows(ev, at(1), at(18), hours=3, top_k=5)
    assert [w.probability_safe for w in windows[:2]] == [1.0, 1.0]
    assert all(
        not a.overlaps(b) for a in windows for b in windows if a is not b
    )
    assert windows[0].start == at(1)
    late = analysis.find_windows(ev, at(13), at(18), hours=3)
    assert late[0].probability_safe == 0.5
    assert analysis.find_windows(ev, at(1), at(3), hours=3) == []


def test_daylight_windows_use_the_site_time_zone():
    london = Site(
        "l", "L", 55.7, -4.25, timezone="Europe/London", hub_height_m=100
    )
    task = config.TaskLimits(
        description="t",
        max_wind_ms=10,
        max_gust_ms=100,
        max_precip_mm_per_h=1,
        min_hours=2,
        work_start_hour=7,
        work_end_hour=9,
    )
    # 24 Oct 2026 is BST (UTC+1): 07-09 local is 06-08 UTC.
    init = datetime(2026, 10, 24, 0, tzinfo=UTC)
    ds = make_ds([[5] * 30] * 2, init=init)
    ev = evaluator(ds, site=london, task=task)
    starts = ev.candidate_starts(init, init + timedelta(hours=30), 2)
    assert starts == [datetime(2026, 10, 24, 6, tzinfo=UTC)]
    assert ev.window(starts[0], 2).to_dict(london)["start_local"] == (
        "2026-10-24T07:00:00+01:00"
    )


def test_window_round_trips_through_dict():
    w = evaluator(make_ds([[5, 5, 5], [5, 5, 5]])).window(at(1), 2)
    again = analysis.Window.from_dict(w.to_dict(SITE))
    assert again.to_dict(SITE) == w.to_dict(SITE)


# --- daily outlook ---------------------------------------------------------


def _six_hourly(days, offset_hours=0, init=INIT):
    steps = days * 4 + 1
    leads = [offset_hours + 6 * i for i in range(steps)]
    winds = [[5.0 + i % 4 for i in range(steps)], [6.0] * steps]
    precs = [[0.5] * steps, [0.0] * steps]  # 0.5 mm/h for 6 h = 3 mm
    return make_ds(winds, precs, lead_hours=leads, init=init)


def test_daily_outlook_keeps_complete_local_days_only():
    ev = evaluator(_six_hourly(2), task=TASK, gust_factor=1.35)
    days = analysis.daily_outlook(ev, at(0), at(48))
    # Each day: wind at 00, 06, 12, 18 (24 h) and rain intervals ending at
    # 06, 12, 18 and 00 the next day (which belongs to the earlier day).
    assert [d["date"] for d in days] == ["2026-11-02", "2026-11-03"]
    day = days[0]
    assert day["precip_total_p90_mm"] == pytest.approx(10.8)  # 12 mm member
    assert day["precip_total_p10_mm"] == pytest.approx(1.2)
    assert day["hub_wind_max_p90_ms"] == pytest.approx(7.8)
    assert day["gust_estimate_max_p90_ms"] == pytest.approx(10.5)
    assert day["prob_any_step_precip_over_limit"] == 0.0
    assert "6-hourly" in day["notes"][0]


def test_daily_outlook_omits_misaligned_coarse_rain_totals():
    # Europe/London in BST: 6-hour intervals end at 01, 07, 13, 19 local.
    london = Site(
        "l", "L", 55.7, -4.25, timezone="Europe/London", hub_height_m=100
    )
    init = datetime(2026, 7, 1, 0, tzinfo=UTC)
    ev = evaluator(_six_hourly(3, init=init), site=london)
    days = analysis.daily_outlook(ev, init, init + timedelta(days=3))
    assert days, "expected complete days"
    for day in days:
        assert "precip_total_p50_mm" not in day
        assert any("midnight" in n for n in day["notes"])


def test_expected_hours_on_dst_change():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/London")
    assert analysis.expected_hours(datetime(2026, 10, 25).date(), tz) == 25
    assert analysis.expected_hours(datetime(2026, 3, 29).date(), tz) == 23


# --- crew planning ---------------------------------------------------------


def make_window(site_id, start_hour, prob, hours=8):
    start = at(start_hour)
    return analysis.Window(
        site_id=site_id,
        start=start,
        end=start + timedelta(hours=hours),
        probability_safe=prob,
        method="ensemble_all_steps",
        meets_go_threshold=prob >= 0.8,
        details={},
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
    assert got["a"][0] == at(40)  # best window for site a
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
    assert got["b"] == at(40)


def test_plan_crews_validation_and_empty_sites():
    with pytest.raises(ValueError):
        analysis.plan_crews({}, crews=0)
    plan = analysis.plan_crews({"a": []}, crews=1)
    assert "no allowed window" in plan["unassigned"]["a"]


def test_percentile_ignores_missing_members():
    assert analysis.percentile(np.array([1.0, np.nan, 3.0]), 50) == 2.0
    with pytest.raises(ValueError):
        analysis.percentile(np.array([np.nan]), 50)


def test_rain_peak_reports_its_interval():
    winds = [[5, 5, 5], [5, 5, 5]]
    precs = [[0, 0, 0], [0, 0, 9]]
    ds = make_ds(winds, precs, lead_hours=[0, 6, 12])
    w = evaluator(ds).window(at(1), 10)
    rain = w.details["precipitation_exceedance"]
    assert rain["peak_step_prob"] == 0.5
    assert rain["peak_step_prob_valid_time_local"] == at(12).isoformat()
    assert rain["peak_step_interval_start_local"] == at(6).isoformat()
