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
"""Unit tests for loaders and configuration. No network or model calls."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import config, forecast
from app.forecast import PRECIP, WIND


def test_sample_forecast_shape():
    sites = forecast.load_sites()
    assert set(sites) == {
        "whitelee",
        "clyde",
        "black-law",
    }
    f = forecast.SampleSource().get(sites["whitelee"])
    assert f.kind == "members"
    assert f.init_time == datetime(2026, 10, 19, 6, tzinfo=UTC)
    assert len(f.times) == 120
    assert f.times[0] == datetime(2026, 10, 20, tzinfo=UTC)
    assert f.times[1] - f.times[0] == timedelta(hours=1)
    # ECMWF IFS ensemble: control + 50 perturbed members.
    assert len(f.members[WIND]) == 51
    assert all(len(m) == 120 for m in f.members[WIND] + f.members[PRECIP])
    assert all(v >= 0 for m in f.members[PRECIP] for v in m)


def test_sample_unknown_site_raises():
    site = forecast.Site("nowhere", "n", 0, 0, 0, 100)
    with pytest.raises(KeyError):
        forecast.SampleSource().get(site)


def test_custom_sites_file(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(
        json.dumps(
            {
                "sites": [
                    {
                        "site_id": "x",
                        "name": "X",
                        "latitude": 1,
                        "longitude": 2,
                        "utc_offset_hours": 0,
                        "hub_height_m": 100,
                    }
                ]
            }
        )
    )
    assert forecast.load_sites(path)["x"].longitude == 2


def test_index_range():
    f = forecast.SampleSource().get(forecast.load_sites()["clyde"])
    lo, hi = f.index_range(f.times[10], f.times[20])
    assert (lo, hi) == (10, 20)
    assert f.index_range(None, None) == (0, 120)


def test_parse_time_defaults_to_utc():
    assert forecast.parse_time("2026-10-12T06:00:00").tzinfo == UTC
    assert forecast.parse_time("2026-10-12T06:00:00Z").hour == 6


@pytest.mark.parametrize(
    "table",
    ["", "dataset.table", "p.d.t`; DROP TABLE x; --", "p.d.t.u", "p.d.t name"],
)
def test_validate_table_rejects_unsafe_ids(table):
    with pytest.raises(ValueError):
        forecast.validate_table(table)


def test_forecast_query_uses_documented_schema():
    sql = forecast.build_forecast_query(
        "my-proj.weathernext.weathernext_3_0_0_0p1deg"
    )
    assert "`my-proj.weathernext.weathernext_3_0_0_0p1deg` AS t" in sql
    assert "CROSS JOIN UNNEST(t.forecast) AS f" in sql
    assert "t.init_time = @init_time" in sql
    assert "ST_INTERSECTS(t.geography_polygon, ST_GEOGPOINT(@lon, @lat))" in sql
    for col in (
        "wind_speed_100m_p10",
        "wind_speed_100m_p90",
        "total_precipitation_1hr_p50",
    ):
        assert f"f.{col}" in sql
    assert "init_time >= TIMESTAMP_SUB" in forecast.build_latest_init_query(
        "p.d.t"
    )


def _rows(init, hours):
    rows = []
    for h in hours:
        row = {"init_time": init, "time": init + timedelta(hours=h), "hours": h}
        for col, level in forecast.QUANTILE_COLUMNS.items():
            row[f"{WIND}_{col}"] = 5.0 + 10 * level
            row[f"{PRECIP}_{col}"] = 0.001 * level  # metres
        rows.append(row)
    return rows


def test_rows_to_forecast_sorts_dedups_and_converts_units():
    init = datetime(2026, 10, 12, tzinfo=UTC)
    rows = _rows(init, [3, 1, 2, 2])
    f = forecast.rows_to_forecast(rows, "s", "bq")
    assert f.kind == "quantiles"
    assert [t.hour for t in f.times] == [1, 2, 3]
    assert f.quantiles[WIND][0.5] == [10.0, 10.0, 10.0]
    assert f.quantiles[PRECIP][0.9] == pytest.approx([0.9, 0.9, 0.9])  # mm
    with pytest.raises(ValueError):
        forecast.rows_to_forecast([], "s", "bq")


class FakeJob:
    def __init__(self, rows):
        self._rows = rows
        self.timeout = None

    def result(self, timeout=None):
        self.timeout = timeout
        return self._rows


class FakeClient:
    def __init__(self, init):
        self.init = init
        self.queries = []
        self.jobs = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        if "MAX(init_time)" in sql:
            job = FakeJob([{"init_time": self.init}])
        else:
            job = FakeJob(_rows(self.init, range(1, 7)))
        self.jobs.append(job)
        return job


def test_bigquery_source_with_fake_client():
    init = datetime(2026, 10, 12, 6, tzinfo=UTC)
    client = FakeClient(init)
    source = forecast.BigQuerySource(
        "p.d.weathernext_3_0_0_0p1deg",
        max_hours=120,
        timeout_seconds=30,
        client=client,
    )
    site = forecast.load_sites()["clyde"]
    f = source.get(site)
    assert f.init_time == init
    assert len(f.times) == 6
    job_config = client.queries[1][1]
    assert int(job_config.job_timeout_ms) == 30000
    assert client.jobs[1].timeout == 30
    params = {p.name: p.value for p in job_config.query_parameters}
    assert params == {
        "init_time": init,
        "lon": -3.6544,
        "lat": 55.4672,
        "max_hours": 120,
    }
    source.get(site)  # cached: no new queries
    assert len(client.queries) == 2


def test_bigquery_source_pinned_init_skips_lookup():
    client = FakeClient(datetime(2026, 10, 12, tzinfo=UTC))
    source = forecast.BigQuerySource(
        "p.d.t",
        max_hours=120,
        timeout_seconds=30,
        init_time="2026-10-12T00:00:00Z",
        client=client,
    )
    source.get(forecast.load_sites()["clyde"])
    assert len(client.queries) == 1


def test_get_source_rejects_unknown_kind(monkeypatch):
    forecast.get_source.cache_clear()
    monkeypatch.setenv("WEATHERNEXT_SOURCE", "csv")
    try:
        with pytest.raises(ValueError):
            forecast.get_source()
    finally:
        forecast.get_source.cache_clear()


def test_task_overrides_inline_and_file(tmp_path):
    tasks = config.load_tasks('{"main_component_lift": {"max_wind_ms": 8.5}}')
    assert tasks["main_component_lift"].max_wind_ms == 8.5
    assert tasks["main_component_lift"].min_hours == 8  # untouched
    path = tmp_path / "limits.json"
    path.write_text(
        json.dumps(
            {
                "cable_pull": {
                    "description": "Ground cable pull",
                    "max_wind_ms": 20,
                    "max_gust_ms": 25,
                    "max_precip_mm": 5,
                    "min_hours": 3,
                }
            }
        )
    )
    assert config.load_tasks(str(path))["cable_pull"].min_hours == 3


def test_task_overrides_reject_bad_input():
    with pytest.raises(ValueError, match="Unknown fields"):
        config.load_tasks('{"blade_repair": {"max_wind": 9}}')
    with pytest.raises(ValueError, match="Incomplete"):
        config.load_tasks('{"new_task": {"max_wind_ms": 9}}')
    assert config.load_tasks("") == config.DEFAULT_TASKS


def test_empty_optional_paths_use_bundled_samples(monkeypatch):
    for var in ("SITES_JSON", "SAMPLE_FORECAST_JSON", "TASK_LIMITS_JSON"):
        monkeypatch.setenv(var, "")
    assert len(forecast.load_sites()) == 3
    assert "Open-Meteo" in forecast.SampleSource().description
    assert config.load_tasks() == config.DEFAULT_TASKS


@pytest.mark.parametrize(
    ("var", "read"),
    [
        ("WEATHERNEXT_SOURCE", forecast.get_source),
        ("GUST_FACTOR", config.gust_factor),
        ("SHEAR_EXPONENT", config.shear_exponent),
    ],
)
def test_required_settings_fail_clearly_when_missing(monkeypatch, var, read):
    monkeypatch.setenv(var, "")
    forecast.get_source.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=var):
            read()
    finally:
        forecast.get_source.cache_clear()


def test_settings_come_from_env_example():
    # tests/conftest.py loads .env.example, the single source of defaults.
    assert config.gust_factor() == 1.35
    assert config.shear_exponent() == 0.14


def test_bigquery_source_requires_table(monkeypatch):
    monkeypatch.setenv("WEATHERNEXT_SOURCE", "bigquery")
    monkeypatch.setenv("WEATHERNEXT_BQ_TABLE", "")
    forecast.get_source.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="WEATHERNEXT_BQ_TABLE"):
            forecast.get_source()
    finally:
        forecast.get_source.cache_clear()


def test_readme_shows_the_exact_bigquery_sql():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    table = "YOUR_PROJECT.YOUR_DATASET.weathernext_3_0_0_0p1deg"
    assert forecast.build_latest_init_query(table) in readme
    assert forecast.build_forecast_query(table) in readme
