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
"""Forecast sources: adapters that return contract datasets of raw members.

Every source returns, per site, an xarray Dataset that follows the forecast
dataset contract (see `contract.py`): unreduced ensemble members, one
`wind_speed` variable at a stated height and one precipitation variable,
each with CF `standard_name`, `units` and `step_type`.

- `SnapshotSource` (default, no credentials): the bundled real ECMWF AIFS ENS
  forecast in `app/data/forecast_snapshot.json`, a point subset of the
  dynamical.org store with native names and attributes.
- `AifsEnsSource`: the same dataset live from dynamical.org (public,
  anonymous Icechunk over HTTPS; needs Python 3.12+ for `icechunk` 2).
- `WeatherNext3Source`: the WeatherNext 3 64-member ensemble Zarr on Google
  Cloud Storage. Needs Application Default Credentials for an allowlisted
  account (see `weathernext_access.py`).

The adapters are the only code that knows dataset-specific variable names.
Wind speed is derived per member from the 100 m wind components before any
statistic is taken, as in the WeatherNext lookup skills.
"""

import dataclasses
import json
import os
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
import xarray as xr

from . import contract
from .config import require_env

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT_FORECAST = DATA_DIR / "forecast_snapshot.json"
SAMPLE_SITES = DATA_DIR / "sites.json"

# Height of the wind components every source reads (closest to hub height).
WIND_HEIGHT_M = 100.0
# Only 6-hourly inits carry the full 15-day horizon in both datasets.
SIX_HOURLY_INIT_HOURS = (0, 6, 12, 18)
# How many inits to walk back when the newest run is still being ingested.
LATEST_INIT_FALLBACKS = 8

AIFS_DATASET_ID = "ecmwf-aifs-ens-forecast"
AIFS_CATALOG_URL = "https://dynamical.org/catalog/ecmwf-aifs-ens-forecast/"
AIFS_ATTRIBUTION = (
    "ECMWF AIFS ENS forecast data processed by dynamical.org from ECMWF Open"
    " Data (CC-BY-4.0)."
)
AIFS_VARIABLES = ("wind_u_100m", "wind_v_100m", "precipitation_surface")

WN3_DATASET_ID = "weathernext-3"
WN3_DISCLAIMER = (
    "WeatherNext 3: experimental forecast provided as-is, not an official"
    " weather warning; see"
    " https://developers.google.com/weathernext/guides/disclaimers"
)
WN3_WIND = ("u_component_of_wind_100m", "v_component_of_wind_100m")
# The WeatherNext lookup skills' default precipitation variable.
WN3_PRECIP = "imerg_tp_1hr"
WN3_GROUP = "res_0p1"
_WN3_INIT_DIR_RE = re.compile(r"(\d{8})_(\d{2})hr_\d+_preds/?$")


@dataclasses.dataclass(frozen=True)
class Site:
    """A wind farm the operations team maintains."""

    site_id: str
    name: str
    latitude: float
    longitude: float
    # IANA time zone, used for working hours and local calendar days.
    timezone: str
    hub_height_m: float
    turbines: int = 0
    type: str = "onshore"
    # Where the coordinates and hub height come from.
    source: str = ""


class ForecastSource(Protocol):
    """Anything that returns a contract dataset for a site."""

    description: str

    def get(self, site: Site) -> xr.Dataset: ...


def parse_time(value: str) -> np.datetime64:
    """Parses an ISO 8601 UTC timestamp to naive `datetime64[ns]`."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return np.datetime64(ts.to_datetime64(), "ns")


def load_sites(path: str | os.PathLike | None = None) -> dict[str, Site]:
    """Loads site metadata from SITES_JSON or the bundled sample."""
    path = Path(path or os.getenv("SITES_JSON") or SAMPLE_SITES)
    doc = json.loads(path.read_text())
    sites = [Site(**s) for s in doc["sites"]]
    return {s.site_id: s for s in sites}


def _wind_speed(
    u: xr.DataArray, v: xr.DataArray, height_m: float
) -> xr.DataArray:
    """Per-member wind speed from vector components (before any statistic)."""
    speed = np.hypot(u, v)
    speed.attrs = {
        "long_name": f"{height_m:g} metre wind speed",
        "standard_name": contract.WIND_SPEED,
        "units": u.attrs.get("units", "m s-1"),
        "step_type": "instant",
        contract.HEIGHT_ATTR: height_m,
    }
    return speed


def _max_lead() -> np.timedelta64:
    hours = int(
        require_env(
            "FORECAST_MAX_LEAD_HOURS", os.getenv("FORECAST_MAX_LEAD_HOURS")
        )
    )
    return np.timedelta64(hours, "h")


# ---------------------------------------------------------------------------
# ECMWF AIFS ENS (dynamical.org)
# ---------------------------------------------------------------------------


def aifs_to_contract(point: xr.Dataset, source: str) -> xr.Dataset:
    """Turns an AIFS ENS point subset (native names) into a contract dataset.

    The dynamical.org store already carries CF attributes (`standard_name`,
    `units`, `step_type`, `realization` on `ensemble_member`); this derives
    the 100 m wind speed per member and keeps precipitation as the native
    6-hour mean rate (`precipitation_flux`, `step_type: avg`).
    """
    u, v = point["wind_u_100m"], point["wind_v_100m"]
    ds = xr.Dataset(
        {
            "wind_speed_100m": _wind_speed(u, v, WIND_HEIGHT_M),
            "precipitation_surface": point["precipitation_surface"],
        }
    )
    ds = ds.drop_vars(
        [
            c
            for c in ds.coords
            if c
            not in (
                "init_time",
                "lead_time",
                "ensemble_member",
                "latitude",
                "longitude",
            )
        ]
    )
    ds["ensemble_member"].attrs.setdefault("standard_name", "realization")
    ds["latitude"].attrs.setdefault("standard_name", "latitude")
    ds["longitude"].attrs.setdefault("standard_name", "longitude")
    ds = ds.transpose("ensemble_member", "lead_time")
    ds.attrs = {
        "title": "ECMWF AIFS ENS",
        "source": source,
        "disclaimer": AIFS_ATTRIBUTION,
        "license": "CC-BY-4.0",
    }
    return ds


class AifsEnsSource:
    """ECMWF AIFS ENS, 51 members, live from dynamical.org (no credentials)."""

    def __init__(self, url: str, init_time: str | None = None):
        self.url = url
        self._init = parse_time(init_time) if init_time else None
        self._store: xr.Dataset | None = None
        self._cache: dict[str, xr.Dataset] = {}
        self.description = (
            "ECMWF AIFS ENS (51 members) live from dynamical.org, from ECMWF"
            " Open Data (CC-BY-4.0)"
        )

    def _open(self) -> xr.Dataset:
        if self._store is None:
            try:
                import icechunk
            except ImportError as e:
                raise RuntimeError(
                    "FORECAST_SOURCE=aifs_ens needs the icechunk package"
                    " (Python 3.12+); use FORECAST_SOURCE=snapshot otherwise"
                ) from e
            repo = icechunk.Repository.open(icechunk.http_storage(self.url))
            session = repo.readonly_session("main")
            self._store = xr.open_zarr(
                session.store, consolidated=False, chunks=None
            )
        return self._store

    def _resolve_init(self, ds: xr.Dataset, site: Site) -> np.datetime64:
        if self._init is not None:
            return self._init
        # The newest run may still be ingesting: take the newest 6-hourly
        # init whose last lead is finite at this site.
        for cand in ds["init_time"].values[::-1][:LATEST_INIT_FALLBACKS]:
            last = (
                ds["wind_u_100m"]
                .sel(
                    init_time=cand,
                    latitude=site.latitude,
                    longitude=site.longitude,
                    method="nearest",
                )
                .isel(lead_time=-1)
                .values
            )
            if np.isfinite(last).all():
                self._init = cand
                return cand
        raise RuntimeError(
            f"no complete AIFS ENS init in the last {LATEST_INIT_FALLBACKS}"
        )

    def get(self, site: Site) -> xr.Dataset:
        if site.site_id not in self._cache:
            ds = self._open()
            init = self._resolve_init(ds, site)
            point = (
                ds[list(AIFS_VARIABLES)]
                .sel(init_time=init)
                .sel(
                    latitude=site.latitude,
                    longitude=site.longitude,
                    method="nearest",
                )
            )
            point = point.sel(lead_time=point["lead_time"] <= _max_lead())
            self._cache[site.site_id] = aifs_to_contract(point.load(), self.url)
        return self._cache[site.site_id]


# ---------------------------------------------------------------------------
# WeatherNext 3 ensembles on Google Cloud Storage
# ---------------------------------------------------------------------------


def _stack_subtime(point: xr.Dataset) -> xr.Dataset:
    """Flattens 6-hourly `lead_time` x hourly `lead_subtime` into hourly leads.

    The publicly documented full-ensemble store uses this layout; rechunked
    stores already have a single hourly `lead_time`.
    """
    if "lead_subtime" not in point.dims:
        return point
    stacked = point.stack(
        step=("lead_time", "lead_subtime"), create_index=False
    )
    leads = stacked["lead_time"].values + stacked["lead_subtime"].values
    stacked = stacked.drop_vars(["lead_time", "lead_subtime"])
    stacked = stacked.assign_coords(lead_time=("step", leads))
    stacked = stacked.swap_dims(step="lead_time").sortby("lead_time")
    _, keep = np.unique(stacked["lead_time"].values, return_index=True)
    return stacked.isel(lead_time=np.sort(keep))


def wn3_to_contract(point: xr.Dataset, source: str) -> xr.Dataset:
    """Turns a WeatherNext 3 ensemble point subset into a contract dataset.

    Mirrors the WeatherNext lookup skill's annotations: `sample` is the
    realization dimension, wind speed is derived per member from the 100 m
    components, and `imerg_tp_1hr` is a 1-hour precipitation accumulation in
    metres (`lwe_thickness_of_precipitation_amount`, `accum`, `PT1H`).
    """
    point = _stack_subtime(point)
    precip = point[WN3_PRECIP].copy()
    precip.attrs = {
        **precip.attrs,
        "standard_name": "lwe_thickness_of_precipitation_amount",
        "units": precip.attrs.get("units", "m"),
        "step_type": "accum",
        "step_period": "PT1H",
    }
    u, v = (point[n] for n in WN3_WIND)
    u.attrs.setdefault("units", "m s-1")
    ds = xr.Dataset(
        {
            "wind_speed_100m": _wind_speed(u, v, WIND_HEIGHT_M),
            WN3_PRECIP: precip,
        }
    )
    keep = {"init_time", "lead_time", "sample", "lat_0p1", "lon_0p1"}
    ds = ds.drop_vars([c for c in ds.coords if c not in keep])
    ds["sample"].attrs.setdefault("standard_name", "realization")
    ds["lat_0p1"].attrs.update(standard_name="latitude", units="degrees_north")
    ds["lon_0p1"].attrs.update(standard_name="longitude", units="degrees_east")
    ds = ds.transpose("sample", "lead_time")
    ds.attrs = {
        "title": "WeatherNext 3",
        "source": source,
        "disclaimer": WN3_DISCLAIMER,
    }
    return ds


def split_gcs_uri(uri: str) -> tuple[str, str]:
    """Splits `gs://bucket/prefix` into (bucket, prefix)."""
    m = re.fullmatch(r"gs://([^/]+)/?(.*)", uri.strip())
    if not m:
        raise ValueError(
            f"WEATHERNEXT_ZARR_URI must be gs://bucket/path: {uri!r}"
        )
    return m.group(1), m.group(2).rstrip("/")


def pick_init_dir(prefixes: list[str], init_time: str | None) -> str:
    """Chooses the per-init folder (`YYYYMMDD_HHhr_XX_preds`) to read.

    Only 6-hourly inits carry the 15-day horizon; hourly interim inits are
    skipped, as in the WeatherNext lookup skill.
    """
    runs = []
    for p in prefixes:
        m = _WN3_INIT_DIR_RE.search(p)
        if m and int(m.group(2)) in SIX_HOURLY_INIT_HOURS:
            runs.append((f"{m.group(1)}{m.group(2)}", p.rstrip("/")))
    if not runs:
        raise RuntimeError("no 6-hourly WeatherNext init folders found")
    if init_time:
        want = pd.Timestamp(parse_time(init_time)).strftime("%Y%m%d%H")
        match = [p for key, p in runs if key == want]
        if not match:
            raise RuntimeError(f"WeatherNext init {init_time} not found")
        return match[-1]
    return max(runs)[1]


class WeatherNext3Source:
    """WeatherNext 3, 64 members, from a Zarr store on Cloud Storage.

    `WEATHERNEXT_ZARR_URI` is either a folder of per-init stores (the public
    full-ensemble layout, `.../2026_to_present`) or one `.zarr` store,
    optionally with the `res_0p1` group. Reads use Application Default
    Credentials; Requester Pays buckets bill `WEATHERNEXT_BILLING_PROJECT`.
    """

    def __init__(
        self,
        uri: str,
        billing_project: str | None = None,
        init_time: str | None = None,
        opener: Callable[[str, str], xr.Dataset] | None = None,
        lister: Callable[[str, str], list[str]] | None = None,
        access_check: Callable[[], None] | None = None,
    ):
        self.bucket, self.prefix = split_gcs_uri(uri)
        self.uri = uri
        self.billing_project = billing_project
        self._init_req = init_time
        self._opener = opener or self._open_zarr
        self._lister = lister or self._list_dirs
        self._access_check = access_check or self._check_access
        self._ds: xr.Dataset | None = None
        self._source = uri
        self._cache: dict[str, xr.Dataset] = {}
        self.description = f"WeatherNext 3 ensemble members (64) from {uri}"

    def _store(self, prefix: str) -> Any:
        from obstore.auth.google import GoogleCredentialProvider
        from obstore.store import GCSStore

        options = {}
        if self.billing_project:
            options["default_headers"] = {
                "x-goog-user-project": self.billing_project
            }
        return GCSStore(
            self.bucket,
            prefix=prefix,
            credential_provider=GoogleCredentialProvider(),
            client_options=options or None,
        )

    def _open_zarr(self, prefix: str, group: str) -> xr.Dataset:
        import zarr

        zstore = zarr.storage.ObjectStore(self._store(prefix), read_only=True)
        return xr.open_zarr(
            zstore, group=group or None, chunks=None, consolidated=None
        )

    def _list_dirs(self, bucket: str, prefix: str) -> list[str]:
        import obstore

        result = obstore.list_with_delimiter(self._store(""), prefix + "/")
        return list(result["common_prefixes"])

    def _check_access(self) -> None:
        from .weathernext_access import check_access

        ok, message = check_access(self.uri, self.billing_project)
        if not ok:
            raise RuntimeError(message)

    def _dataset(self) -> xr.Dataset:
        if self._ds is not None:
            return self._ds
        self._access_check()
        prefix = self.prefix
        if not prefix.endswith(".zarr"):
            run = pick_init_dir(
                self._lister(self.bucket, prefix), self._init_req
            )
            prefix = f"{run}/predictions.zarr"
        ds = self._opener(prefix, "")
        if WN3_PRECIP not in ds.data_vars:
            ds = self._opener(prefix, WN3_GROUP)
        if "init_time" in ds.dims:
            ds = ds.sel(init_time=self._pick_init(ds))
        self._source = f"gs://{self.bucket}/{prefix}"
        self._ds = ds
        return ds

    def _pick_init(self, ds: xr.Dataset) -> np.datetime64:
        if self._init_req:
            return parse_time(self._init_req)
        inits = pd.DatetimeIndex(ds["init_time"].values)
        ok = np.isin(inits.hour, SIX_HOURLY_INIT_HOURS)
        if "is_processed" in ds:
            ok &= np.asarray(ds["is_processed"].values) != 0
        if not ok.any():
            raise RuntimeError("no processed 6-hourly WeatherNext init found")
        return np.datetime64(inits[ok][-1].to_datetime64(), "ns")

    def get(self, site: Site) -> xr.Dataset:
        if site.site_id not in self._cache:
            ds = self._dataset()
            point = ds[[*WN3_WIND, WN3_PRECIP]].sel(
                lat_0p1=site.latitude,
                lon_0p1=site.longitude % 360,
                method="nearest",
            )
            out = wn3_to_contract(point.load(), self._source)
            out = out.sel(lead_time=out["lead_time"] <= _max_lead())
            self._cache[site.site_id] = out
        return self._cache[site.site_id]


# ---------------------------------------------------------------------------
# Bundled snapshot
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, Callable[[xr.Dataset, str], xr.Dataset]] = {
    AIFS_DATASET_ID: aifs_to_contract,
    WN3_DATASET_ID: wn3_to_contract,
}


def snapshot_point(doc: dict[str, Any], site_id: str) -> xr.Dataset:
    """Rebuilds the native point subset for one site from a snapshot file."""
    site = doc["sites"].get(site_id)
    if site is None:
        raise KeyError(f"No snapshot forecast for site {site_id!r}")
    real = doc["realization"]
    leads = (
        np.array(doc["lead_time_hours"], dtype="int64")
        .astype("timedelta64[h]")
        .astype("timedelta64[ns]")
    )
    coord_attrs = doc.get("coordinate_attrs", {})
    data_vars = {
        name: xr.DataArray(
            np.array(values, dtype="float64"),
            dims=(real["name"], "lead_time"),
            attrs=doc["variable_attrs"][name],
        )
        for name, values in site["data"].items()
    }
    coords = {
        real["name"]: (real["name"], real["values"], real["attrs"]),
        "lead_time": ("lead_time", leads, coord_attrs.get("lead_time", {})),
        "init_time": (
            (),
            parse_time(doc["init_time"]),
            coord_attrs.get("init_time", {}),
        ),
        "latitude": (
            (),
            site["latitude"],
            coord_attrs.get("latitude", {"standard_name": "latitude"}),
        ),
        "longitude": (
            (),
            site["longitude"],
            coord_attrs.get("longitude", {"standard_name": "longitude"}),
        ),
    }
    # Null entries (for example precipitation at lead 0) become NaN.
    return xr.Dataset(data_vars, coords=coords, attrs=doc["dataset_attrs"])


class SnapshotSource:
    """Reads a bundled (or user-supplied) point snapshot of raw members."""

    def __init__(self, path: str | os.PathLike | None = None):
        path = Path(
            path or os.getenv("FORECAST_SNAPSHOT_JSON") or SNAPSHOT_FORECAST
        )
        self._doc = json.loads(path.read_text())
        dataset_id = self._doc["dataset_attrs"]["dataset_id"]
        if dataset_id not in _ADAPTERS:
            raise ValueError(f"No adapter for snapshot dataset {dataset_id!r}")
        self._adapter = _ADAPTERS[dataset_id]
        self.provenance = self._doc.get("provenance", {})
        self.description = self._doc.get(
            "description", f"snapshot file {path.name}"
        )

    def get(self, site: Site) -> xr.Dataset:
        point = snapshot_point(self._doc, site.site_id)
        return self._adapter(
            point, self.provenance.get("source", "bundled snapshot")
        )


@lru_cache(maxsize=1)
def get_source() -> ForecastSource:
    """Returns the configured forecast source (FORECAST_SOURCE)."""
    kind = (
        require_env("FORECAST_SOURCE", os.getenv("FORECAST_SOURCE"))
        .strip()
        .lower()
    )
    init_time = os.getenv("FORECAST_INIT_TIME") or None
    if kind == "snapshot":
        return SnapshotSource()
    if kind == "aifs_ens":
        return AifsEnsSource(
            require_env(
                "AIFS_ENS_ICECHUNK_URL", os.getenv("AIFS_ENS_ICECHUNK_URL")
            ),
            init_time=init_time,
        )
    if kind == "weathernext_3":
        return WeatherNext3Source(
            require_env(
                "WEATHERNEXT_ZARR_URI", os.getenv("WEATHERNEXT_ZARR_URI")
            ),
            billing_project=os.getenv("WEATHERNEXT_BILLING_PROJECT") or None,
            init_time=init_time,
        )
    raise ValueError(
        "FORECAST_SOURCE must be 'snapshot', 'aifs_ens' or 'weathernext_3',"
        f" got {kind!r}"
    )


def describe(ds: xr.Dataset) -> dict[str, Any]:
    """Short, JSON-friendly description of a contract dataset."""
    times = contract.valid_times(ds)
    leads = pd.to_timedelta(ds["lead_time"].values)
    spacing = sorted(
        {int(h) for h in np.diff(leads).astype("timedelta64[h]").astype(int)}
    )
    lat, lon = contract.point_location(ds)
    return {
        "dataset": ds.attrs.get("title", ""),
        "init_time": contract.init_time(ds).isoformat(),
        "members": int(ds.sizes[contract.realization_dim(ds)]),
        "steps": len(times),
        "step_hours": spacing,
        "first_valid_time": times[0].isoformat(),
        "last_valid_time": times[-1].isoformat(),
        "grid_point": [round(lat, 3), round(lon, 3)],
        "attribution": ds.attrs.get("disclaimer", ""),
    }
