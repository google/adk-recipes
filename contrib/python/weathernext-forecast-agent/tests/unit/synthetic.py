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
"""Builds small contract datasets for unit tests."""

from datetime import UTC, datetime

import numpy as np
import xarray as xr

INIT = datetime(2026, 11, 2, 0, tzinfo=UTC)


def make_ds(
    winds,
    precs=None,
    *,
    lead_hours=None,
    precip_kind="rate",
    wind_units="m s-1",
    wind_name="ws",
    precip_name="pr",
    member_dim="number",
    height=100.0,
    init=INIT,
) -> xr.Dataset:
    """Contract dataset with one wind_speed and one precipitation variable.

    Args:
        winds: [member][step] wind speed in `wind_units`.
        precs: [member][step] precipitation: mm/h rates (`precip_kind`
            "rate", step_type avg) or mm per 1 h step ("amount", accum PT1H).
        lead_hours: lead time of each step; default 1, 2, 3, ...
    """
    winds = np.asarray(winds, dtype=float)
    n_members, n_steps = winds.shape
    precs = np.zeros_like(winds) if precs is None else np.asarray(precs, float)
    leads = lead_hours or list(range(1, n_steps + 1))
    wind_attrs = {
        "standard_name": "wind_speed",
        "units": wind_units,
        "step_type": "instant",
        "height_above_ground_m": height,
    }
    if precip_kind == "rate":
        precip_attrs = {
            "standard_name": "precipitation_flux",
            "units": "kg m-2 s-1",
            "step_type": "avg",
        }
        precs = precs / 3600.0  # mm/h -> kg m-2 s-1
    else:
        precip_attrs = {
            "standard_name": "lwe_thickness_of_precipitation_amount",
            "units": "m",
            "step_type": "accum",
            "step_period": "PT1H",
        }
        precs = precs / 1000.0  # mm -> m
    dims = (member_dim, "lead_time")
    return xr.Dataset(
        {
            wind_name: (dims, winds, wind_attrs),
            precip_name: (dims, precs, precip_attrs),
        },
        coords={
            member_dim: (
                member_dim,
                np.arange(n_members),
                {"standard_name": "realization"},
            ),
            "lead_time": (
                "lead_time",
                np.array(leads, dtype="int64").astype("timedelta64[h]"),
            ),
            "init_time": np.datetime64(init.replace(tzinfo=None), "ns"),
            "lat": ((), 55.7, {"standard_name": "latitude"}),
            "lon": ((), 355.75, {"standard_name": "longitude"}),
        },
        attrs={"title": "test", "disclaimer": "synthetic"},
    )
