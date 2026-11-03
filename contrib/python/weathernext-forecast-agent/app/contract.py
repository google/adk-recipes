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
"""Forecast dataset contract: metadata lookups and CF unit conversion.

Every data source in `forecast.py` is an adapter that returns an in-memory
xarray Dataset following the WeatherNext team's forecast dataset contract:

- Variables are identified by their CF `standard_name`, never by name, and
  carry `units` and `step_type` (`instant`, `accum`, `avg`, `min`, `max`).
  `accum`/`min`/`max` variables also carry an ISO 8601 `step_period`
  (`PT1H`); `avg` intervals come from the `lead_time` spacing.
- Unreduced members sit on the dimension of the coordinate whose
  `standard_name` is `realization`. Reduced variables carry a `statistic`
  attribute and are rejected here, because probabilities need members.
- `init_time` (scalar `datetime64`) and `lead_time` (1D `timedelta64`) give
  the valid time `init_time + lead_time`; lead spacing may be non-uniform.
- Latitude and longitude are found by `standard_name`.

The analysis code (`analysis.py`) only uses the helpers below, so any dataset
that satisfies the contract (WeatherNext 3, ECMWF AIFS ENS, your own) goes
through the same path.
"""

import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pandas as pd
import xarray as xr

REALIZATION = "realization"
WIND_SPEED = "wind_speed"
# CF standard names for precipitation, by step semantics.
PRECIP_AMOUNT_NAMES = (
    "precipitation_amount",
    "lwe_thickness_of_precipitation_amount",
)
PRECIP_RATE_NAMES = ("precipitation_flux", "lwe_precipitation_rate")
STEP_TYPES = ("instant", "accum", "avg", "min", "max")
# Step types whose value describes the interval ending at the valid time.
INTERVAL_STEP_TYPES = ("accum", "avg", "min", "max")
# Recipe extension: height of a wind variable above ground, in metres.
HEIGHT_ATTR = "height_above_ground_m"


class ContractError(ValueError):
    """The dataset does not satisfy the forecast dataset contract."""


def _norm_units(units: str) -> str:
    u = units.strip().lower().replace("**", "").replace("^", "")
    return re.sub(r"\s+", " ", u)


# Conversion factors to the recipe's working units, keyed by normalised unit.
_WIND_TO_MS = {
    "m s-1": 1.0,
    "m/s": 1.0,
    "km/h": 1 / 3.6,
    "km h-1": 1 / 3.6,
    "kph": 1 / 3.6,
    "knots": 0.514444,
    "knot": 0.514444,
    "kt": 0.514444,
    "mph": 0.44704,
}
_AMOUNT_TO_MM = {
    "m": 1000.0,
    "mm": 1.0,
    "kg m-2": 1.0,
    "inches": 25.4,
    "inch": 25.4,
    "in": 25.4,
}
_RATE_TO_MM_PER_H = {
    "kg m-2 s-1": 3600.0,
    "mm/s": 3600.0,
    "mm s-1": 3600.0,
    "mm/h": 1.0,
    "mm/hr": 1.0,
    "mm h-1": 1.0,
    "m/s": 3.6e6,
    "m s-1": 3.6e6,
    "in/h": 25.4,
}


def _factor(da: xr.DataArray, table: dict[str, float], target: str) -> float:
    units = da.attrs.get("units")
    if not units:
        raise ContractError(f"variable {da.name!r} has no 'units' attribute")
    try:
        return table[_norm_units(str(units))]
    except KeyError:
        raise ContractError(
            f"cannot convert {da.name!r} from {units!r} to {target}"
        ) from None


def to_units(da: xr.DataArray, target: str) -> xr.DataArray:
    """Converts a variable to `m/s`, `mm` or `mm/h` using its CF metadata.

    The category comes from `standard_name`, so an accumulation is never
    turned into a rate or the other way round.
    """
    name = da.attrs.get("standard_name")
    if target == "m/s" and name == WIND_SPEED:
        table = _WIND_TO_MS
    elif target == "mm" and name in PRECIP_AMOUNT_NAMES:
        table = _AMOUNT_TO_MM
    elif target == "mm/h" and name in PRECIP_RATE_NAMES:
        table = _RATE_TO_MM_PER_H
    else:
        raise ContractError(
            f"{da.name!r} (standard_name={name!r}) cannot be expressed in"
            f" {target}"
        )
    out = da * _factor(da, table, target)
    out.attrs = {**da.attrs, "units": target}
    return out


def _coord_by_standard_name(ds: xr.Dataset, name: str) -> str:
    for coord, values in ds.coords.items():
        if values.attrs.get("standard_name") == name:
            return str(coord)
    raise ContractError(f"no coordinate with standard_name={name!r}")


def realization_dim(ds: xr.Dataset) -> str:
    """Name of the unreduced ensemble dimension (size > 1)."""
    dim = _coord_by_standard_name(ds, REALIZATION)
    if dim not in ds.dims or ds.sizes[dim] < 2:
        raise ContractError(
            "ensemble probabilities need an unreduced realization dimension"
            f" with more than one member; {dim!r} has"
            f" {ds.sizes.get(dim, 0)}"
        )
    return dim


def point_location(ds: xr.Dataset) -> tuple[float, float]:
    """Scalar (latitude, longitude) of a point extract, by standard_name."""
    lat = ds[_coord_by_standard_name(ds, "latitude")]
    lon = ds[_coord_by_standard_name(ds, "longitude")]
    if lat.size != 1 or lon.size != 1:
        raise ContractError("expected a single-point extract")
    lon_val = float(lon.values.reshape(-1)[0])
    if lon_val > 180:
        lon_val -= 360
    return float(lat.values.reshape(-1)[0]), lon_val


def init_time(ds: xr.Dataset) -> datetime:
    """Forecast initialization time (UTC)."""
    if "init_time" not in ds.coords:
        raise ContractError("missing init_time coordinate")
    value = np.atleast_1d(ds["init_time"].values).reshape(-1)[0]
    return pd.Timestamp(value).to_pydatetime().replace(tzinfo=UTC)


def valid_times(ds: xr.Dataset) -> list[datetime]:
    """Valid times `init_time + lead_time` (UTC)."""
    if "lead_time" not in ds.dims:
        raise ContractError("missing lead_time dimension")
    base = init_time(ds)
    return [
        base + pd.Timedelta(v).to_pytimedelta() for v in ds["lead_time"].values
    ]


def step_intervals(ds: xr.Dataset, da: xr.DataArray) -> list[timedelta]:
    """Length of the period each step of `da` stands for.

    Interval variables (`accum`, `avg`, `min`, `max`) describe the period
    ending at the valid time: `step_period` when given, else the gap to the
    previous lead (the first step reuses the first gap). Instantaneous
    variables stand for the gap to the next lead (the last step reuses the
    last gap), as in the WeatherNext daily-summary completeness rule.
    """
    leads = [pd.Timedelta(v).to_pytimedelta() for v in ds["lead_time"].values]
    if len(leads) < 2:
        return [timedelta(0)] * len(leads)
    gaps = [b - a for a, b in pairwise(leads)]
    if da.attrs.get("step_type") in INTERVAL_STEP_TYPES:
        period = da.attrs.get("step_period")
        if period:
            return [pd.Timedelta(period).to_pytimedelta()] * len(leads)
        return [gaps[0], *gaps]
    return [*gaps, gaps[-1]]


def find_variable(
    ds: xr.Dataset, standard_names: tuple[str, ...] | str
) -> xr.DataArray:
    """The single data variable with one of `standard_names`."""
    names = (
        (standard_names,) if isinstance(standard_names, str) else standard_names
    )
    found = [
        ds[v] for v in ds.data_vars if ds[v].attrs.get("standard_name") in names
    ]
    if len(found) != 1:
        raise ContractError(
            f"expected exactly one variable with standard_name in {names},"
            f" found {[str(d.name) for d in found]}"
        )
    return found[0]


def validate(ds: xr.Dataset) -> None:
    """Checks the parts of the contract the analysis relies on.

    Raises:
        ContractError: If anything required is missing.
    """
    member_dim = realization_dim(ds)
    point_location(ds)
    valid_times(ds)
    for name in ds.data_vars:
        attrs = ds[name].attrs
        for key in ("standard_name", "units", "step_type"):
            if not attrs.get(key):
                raise ContractError(f"variable {name!r} has no {key!r}")
        if attrs.get("statistic"):
            raise ContractError(
                f"variable {name!r} is a reduced statistic"
                f" ({attrs['statistic']}); probabilities need raw members"
            )
        if attrs["step_type"] not in STEP_TYPES:
            raise ContractError(
                f"variable {name!r} has unknown step_type {attrs['step_type']!r}"
            )
        if attrs["step_type"] in ("accum", "min", "max") and not attrs.get(
            "step_period"
        ):
            raise ContractError(
                f"variable {name!r} ({attrs['step_type']}) has no step_period"
            )
        if set(ds[name].dims) != {member_dim, "lead_time"}:
            raise ContractError(
                f"variable {name!r} must have dims ({member_dim}, lead_time),"
                f" got {ds[name].dims}"
            )
