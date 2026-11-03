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
"""Unit tests for the forecast adapters, snapshot, access check and config."""

import json

import numpy as np
import pytest
import xarray as xr

from app import analysis, config, contract, forecast, weathernext_access

SITES = forecast.load_sites()


@pytest.fixture
def clean_source():
    forecast.get_source.cache_clear()
    yield
    forecast.get_source.cache_clear()


# --- bundled snapshot ------------------------------------------------------


def test_snapshot_is_a_contract_dataset_of_raw_members():
    src = forecast.SnapshotSource()
    ds = src.get(SITES["whitelee"])
    contract.validate(ds)
    assert contract.realization_dim(ds) == "ensemble_member"
    assert ds.sizes["ensemble_member"] == 51
    wind = contract.find_variable(ds, contract.WIND_SPEED)
    assert wind.attrs[contract.HEIGHT_ATTR] == 100.0
    rain = contract.find_variable(ds, contract.PRECIP_RATE_NAMES)
    assert (rain.attrs["units"], rain.attrs["step_type"]) == (
        "kg m-2 s-1",
        "avg",
    )
    # Precipitation is a mean over the previous step: missing at lead 0.
    assert np.isnan(rain.isel(lead_time=0)).all()
    meta = forecast.describe(ds)
    assert meta["init_time"] == "2026-10-20T06:00:00+00:00"
    assert meta["step_hours"] == [6]
    assert meta["grid_point"] == [55.75, -4.25]
    assert "dynamical.org" in meta["attribution"]


def test_snapshot_records_provenance_and_licence():
    src = forecast.SnapshotSource()
    prov = src.provenance
    assert prov["dataset_id"] == forecast.AIFS_DATASET_ID
    assert prov["source"].endswith("v0.1.0.icechunk")
    assert "CC-BY-4.0" in prov["license"]
    assert "ECMWF Open Data" in prov["attribution"]
    assert "AIFS ENS" in src.description


def test_snapshot_unknown_site_raises():
    with pytest.raises(KeyError):
        forecast.SnapshotSource().get(
            forecast.Site("x", "X", 0, 0, timezone="UTC", hub_height_m=80)
        )


def test_custom_sites_file(tmp_path):
    path = tmp_path / "sites.json"
    site = {
        "site_id": "s",
        "name": "S",
        "latitude": 1.0,
        "longitude": 2.0,
        "timezone": "Europe/Berlin",
        "hub_height_m": 120,
    }
    path.write_text(json.dumps({"sites": [site]}))
    assert forecast.load_sites(path)["s"].timezone == "Europe/Berlin"


# --- adapters --------------------------------------------------------------


def _aifs_native(members=3, leads=(0, 6, 12)):
    shape = (len(leads), members)
    attrs_u = {
        "standard_name": "eastward_wind",
        "units": "m s-1",
        "step_type": "instant",
    }
    attrs_v = {**attrs_u, "standard_name": "northward_wind"}
    attrs_p = {
        "standard_name": "precipitation_flux",
        "units": "kg m-2 s-1",
        "step_type": "avg",
    }
    dims = ("lead_time", "ensemble_member")
    return xr.Dataset(
        {
            "wind_u_100m": (dims, np.full(shape, 3.0), attrs_u),
            "wind_v_100m": (dims, np.full(shape, 4.0), attrs_v),
            "precipitation_surface": (dims, np.full(shape, 1e-4), attrs_p),
        },
        coords={
            "lead_time": np.array(leads, dtype="int64").astype(
                "timedelta64[h]"
            ),
            "ensemble_member": (
                "ensemble_member",
                np.arange(members),
                {"standard_name": "realization"},
            ),
            "init_time": np.datetime64("2026-10-20T06:00", "ns"),
            "latitude": ((), 55.75, {"standard_name": "latitude"}),
            "longitude": ((), -4.25, {"standard_name": "longitude"}),
            "spatial_ref": 0,
        },
    )


def test_aifs_adapter_derives_member_wind_speed():
    ds = forecast.aifs_to_contract(_aifs_native(), "src")
    contract.validate(ds)
    assert float(ds["wind_speed_100m"].max()) == 5.0  # hypot(3, 4)
    assert "spatial_ref" not in ds.coords
    assert ds.attrs["license"] == "CC-BY-4.0"
    assert ds["wind_speed_100m"].dims == ("ensemble_member", "lead_time")


def test_aifs_source_reads_latest_complete_init(monkeypatch):
    store = _aifs_native(leads=(0, 6, 12, 18)).expand_dims(
        init_time=[
            np.datetime64("2026-10-20T00", "ns"),
            np.datetime64("2026-10-20T06", "ns"),
        ]
    )
    store = store.expand_dims(latitude=[55.75], longitude=[-4.25])
    # The newest init is still ingesting: its last lead is missing.
    u = store["wind_u_100m"]
    values = u.values.copy()
    values[:, :, 1, -1, :] = np.nan
    store["wind_u_100m"] = (u.dims, values, u.attrs)
    monkeypatch.setenv("FORECAST_MAX_LEAD_HOURS", "12")
    src = forecast.AifsEnsSource("https://example/store.icechunk")
    monkeypatch.setattr(src, "_open", lambda: store)
    ds = src.get(SITES["whitelee"])
    assert contract.init_time(ds).isoformat() == "2026-10-20T00:00:00+00:00"
    assert ds.sizes["lead_time"] == 3  # 0, 6, 12 h
    analysis.SiteForecast.from_dataset(ds, SITES["whitelee"])


def _wn3_native(subtime=False):
    members, dims = 2, ("sample", "lead_time")
    if subtime:
        leads = np.array([6, 12], dtype="int64").astype("timedelta64[h]")
        shape: tuple[int, ...] = (members, 2, 6)
        dims = ("sample", "lead_time", "lead_subtime")
        extra = {
            "lead_subtime": np.arange(6).astype("timedelta64[h]"),
        }
    else:
        leads = np.arange(1, 5).astype("timedelta64[h]")
        shape = (members, 4)
        extra = {}
    return xr.Dataset(
        {
            "u_component_of_wind_100m": (dims, np.full(shape, 6.0)),
            "v_component_of_wind_100m": (dims, np.full(shape, 8.0)),
            "imerg_tp_1hr": (dims, np.full(shape, 0.0004), {"units": "m"}),
        },
        coords={
            "sample": np.arange(members),
            "lead_time": leads,
            "init_time": np.datetime64("2026-10-20T06", "ns"),
            "lat_0p1": ((), 55.7),
            "lon_0p1": ((), 355.7),
            **extra,
        },
    )


def test_wn3_adapter_annotates_the_contract():
    ds = forecast.wn3_to_contract(_wn3_native(), "gs://b/p")
    contract.validate(ds)
    assert contract.realization_dim(ds) == "sample"
    assert contract.point_location(ds) == pytest.approx((55.7, -4.3))
    rain = ds["imerg_tp_1hr"]
    assert (rain.attrs["step_type"], rain.attrs["step_period"]) == (
        "accum",
        "PT1H",
    )
    f = analysis.SiteForecast.from_dataset(ds, SITES["whitelee"], 0.0)
    assert f.precip_units == "mm"
    assert f.precip[0, 0] == pytest.approx(0.4)
    assert f.hub_wind[0, 0] == pytest.approx(10.0)
    assert "disclaimers" in ds.attrs["disclaimer"]


def test_wn3_adapter_flattens_lead_subtime_to_hourly_steps():
    ds = forecast.wn3_to_contract(_wn3_native(subtime=True), "gs://b/p")
    hours = (ds["lead_time"].values / np.timedelta64(1, "h")).astype(int)
    assert hours.tolist() == list(range(6, 18))


def test_wn3_source_picks_latest_six_hourly_run_and_res_group(monkeypatch):
    monkeypatch.setenv("FORECAST_MAX_LEAD_HOURS", "360")
    opened = []
    point = (
        _wn3_native()
        .drop_vars(["lat_0p1", "lon_0p1"])
        .expand_dims(lat_0p1=[55.7], lon_0p1=[355.7])
    )

    def opener(prefix, group):
        opened.append((prefix, group))
        return point if group == "res_0p1" else xr.Dataset()

    runs = [
        "root/20261020_06hr_01_preds/",
        "root/20261020_07hr_01_preds/",  # interim run, skipped
        "root/20261020_00hr_01_preds/",
    ]
    src = forecast.WeatherNext3Source(
        "gs://bucket/root",
        opener=opener,
        lister=lambda bucket, prefix: runs,
        access_check=lambda: None,
    )
    ds = src.get(SITES["whitelee"])
    assert opened == [
        ("root/20261020_06hr_01_preds/predictions.zarr", ""),
        ("root/20261020_06hr_01_preds/predictions.zarr", "res_0p1"),
    ]
    assert ds.attrs["source"] == (
        "gs://bucket/root/20261020_06hr_01_preds/predictions.zarr"
    )


def test_wn3_source_stops_when_access_is_denied():
    def denied():
        raise RuntimeError("not on the WeatherNext data allowlist")

    src = forecast.WeatherNext3Source(
        "gs://bucket/store.zarr", access_check=denied
    )
    with pytest.raises(RuntimeError, match="allowlist"):
        src.get(SITES["whitelee"])


def test_pick_init_dir_and_uri_parsing():
    runs = ["p/20261019_18hr_01_preds/", "p/20261020_00hr_01_preds/"]
    assert forecast.pick_init_dir(runs, None) == "p/20261020_00hr_01_preds"
    assert (
        forecast.pick_init_dir(runs, "2026-10-19T18:00:00Z")
        == "p/20261019_18hr_01_preds"
    )
    with pytest.raises(RuntimeError):
        forecast.pick_init_dir(runs, "2026-10-21T00:00:00Z")
    assert forecast.split_gcs_uri("gs://b/a/b/") == ("b", "a/b")
    with pytest.raises(ValueError):
        forecast.split_gcs_uri("s3://b/a")


# --- access check ----------------------------------------------------------


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class FakeSession:
    def __init__(self, status, text=""):
        self.response = FakeResponse(status, text)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        return self.response


@pytest.mark.parametrize(
    ("status", "text", "ok", "needle"),
    [
        (200, "", True, "confirmed"),
        (401, "", False, "application-default login"),
        (403, "", False, "allowlist"),
        (
            400,
            "Bucket is a requester pays bucket but no user project",
            False,
            "WEATHERNEXT_BILLING_PROJECT",
        ),
        (500, "boom", False, "Unexpected HTTP 500"),
    ],
)
def test_access_check_explains_each_outcome(status, text, ok, needle):
    session = FakeSession(status, text)
    got_ok, message = weathernext_access.check_access(
        "gs://bucket/prefix", "my-project", session=session
    )
    assert got_ok is ok
    assert needle in message
    url, params = session.calls[0]
    assert url.endswith("/b/bucket/o")
    assert params == {
        "maxResults": "1",
        "prefix": "prefix",
        "userProject": "my-project",
    }
    if status == 403:
        assert weathernext_access.ACCESS_FORM_URL in message


# --- configuration -----------------------------------------------------------


@pytest.mark.usefixtures("clean_source")
@pytest.mark.parametrize(
    ("kind", "cls"),
    [
        ("snapshot", forecast.SnapshotSource),
        ("aifs_ens", forecast.AifsEnsSource),
        ("weathernext_3", forecast.WeatherNext3Source),
    ],
)
def test_get_source_kinds(monkeypatch, kind, cls):
    monkeypatch.setenv("FORECAST_SOURCE", kind)
    assert isinstance(forecast.get_source(), cls)


@pytest.mark.usefixtures("clean_source")
def test_get_source_rejects_unknown_kind(monkeypatch):
    monkeypatch.setenv("FORECAST_SOURCE", "bigquery")
    with pytest.raises(ValueError, match="FORECAST_SOURCE"):
        forecast.get_source()


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
                    "max_precip_mm_per_h": 5,
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


def test_empty_optional_paths_use_bundled_files(monkeypatch):
    for var in ("SITES_JSON", "FORECAST_SNAPSHOT_JSON", "TASK_LIMITS_JSON"):
        monkeypatch.setenv(var, "")
    assert len(forecast.load_sites()) == 3
    assert "AIFS ENS" in forecast.SnapshotSource().description
    assert config.load_tasks() == config.DEFAULT_TASKS


@pytest.mark.usefixtures("clean_source")
@pytest.mark.parametrize(
    ("var", "kind", "read"),
    [
        ("FORECAST_SOURCE", "", forecast.get_source),
        ("AIFS_ENS_ICECHUNK_URL", "aifs_ens", forecast.get_source),
        ("WEATHERNEXT_ZARR_URI", "weathernext_3", forecast.get_source),
        ("GUST_FACTOR", "snapshot", config.gust_factor),
        ("SHEAR_EXPONENT", "snapshot", config.shear_exponent),
    ],
)
def test_required_settings_fail_clearly_when_missing(
    monkeypatch, var, kind, read
):
    monkeypatch.setenv("FORECAST_SOURCE", kind)
    monkeypatch.setenv(var, "")
    with pytest.raises(RuntimeError, match=var):
        read()


def test_settings_come_from_env_example():
    # tests/conftest.py loads .env.example, the single source of defaults.
    assert config.gust_factor() == 1.35
    assert config.shear_exponent() == 0.14
