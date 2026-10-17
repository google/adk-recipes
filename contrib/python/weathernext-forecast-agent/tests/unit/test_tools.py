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
"""Unit tests for the agent tools on the bundled sample. No model calls."""

import pytest

from app import agent, forecast


@pytest.fixture(autouse=True)
def sample_source(monkeypatch):
    monkeypatch.setenv("WEATHERNEXT_SOURCE", "sample")
    monkeypatch.delenv("TASK_LIMITS_JSON", raising=False)
    forecast.get_source.cache_clear()
    yield
    forecast.get_source.cache_clear()


def test_list_sites():
    result = agent.list_sites()
    assert len(result["sites"]) == 4
    assert {"main_component_lift", "blade_repair", "nacelle_service"} <= set(
        result["task_types"]
    )
    assert "Not real WeatherNext data" in result["data_source"]


def test_forecast_summary_blocks():
    result = agent.get_forecast_summary(
        "prairie-ridge", horizon_hours=48, block_hours=12
    )
    assert result["data_kind"] == "members"
    assert len(result["blocks"]) == 4
    for b in result["blocks"]:
        assert (
            b["hub_wind_max_p10_ms"]
            <= b["hub_wind_max_p50_ms"]
            <= b["hub_wind_max_p90_ms"]
        )


def test_find_windows_sample_results():
    result = agent.find_maintenance_windows(
        "prairie-ridge", "main_component_lift"
    )
    windows = result["windows"]
    assert result["required_hours"] == 8
    assert windows
    probs = [w["probability_safe"] for w in windows]
    assert probs == sorted(probs, reverse=True)
    best = windows[0]
    assert best["meets_go_threshold"]
    assert best["members_total"] == 32
    assert best["start_local"].endswith("-05:00")
    hour = int(best["start_local"][11:13])
    assert 7 <= hour <= 11  # 8-hour window inside 07:00-19:00 local


def test_find_windows_custom_hours_and_start():
    result = agent.find_maintenance_windows(
        "elbe-marsh",
        "blade_repair",
        min_hours=4,
        start="2026-10-14T00:00:00Z",
        horizon_hours=48,
        top_k=2,
    )
    assert result["required_hours"] == 4
    assert len(result["windows"]) <= 2
    assert all(w["start_utc"] >= "2026-10-14" for w in result["windows"])


def test_task_override_changes_result(monkeypatch):
    base = agent.find_maintenance_windows("elbe-marsh", "main_component_lift")
    monkeypatch.setenv(
        "TASK_LIMITS_JSON", '{"main_component_lift": {"max_wind_ms": 6}}'
    )
    strict = agent.find_maintenance_windows("elbe-marsh", "main_component_lift")
    assert (
        strict["windows"][0]["probability_safe"]
        < base["windows"][0]["probability_safe"]
    )


def test_tool_errors_are_returned_not_raised():
    assert "error" in agent.find_maintenance_windows("atlantis", "blade_repair")
    assert "error" in agent.find_maintenance_windows("elbe-marsh", "painting")
    assert "error" in agent.get_forecast_summary("atlantis")
    assert "error" in agent.plan_crew_schedule("painting", 1)
    assert "error" in agent.plan_crew_schedule("blade_repair", 0)
    assert "error" in agent.plan_crew_schedule(
        "blade_repair", 1, site_ids="atlantis"
    )


def test_plan_crew_schedule_on_sample():
    result = agent.plan_crew_schedule("main_component_lift", crews=2)
    assigned = {a["site_id"] for a in result["assignments"]}
    assert "prairie-ridge" in assigned
    assert set(result["unassigned_sites"]) | assigned == {
        "prairie-ridge",
        "highland-moor",
        "elbe-marsh",
        "dogger-offshore",
    }
    assert all(a["probability_safe"] >= 0.8 for a in result["assignments"])
    relaxed = agent.plan_crew_schedule(
        "main_component_lift", crews=2, min_probability=0.5
    )
    assert len(relaxed["assignments"]) > len(result["assignments"])
