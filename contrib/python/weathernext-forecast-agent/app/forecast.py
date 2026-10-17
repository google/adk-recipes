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
"""Forecast data model and loaders.

Two sources produce the same `Forecast` object:

- `SampleSource` (default): the bundled synthetic ensemble in
  `app/data/sample_forecast.json`, with individual ensemble members.
- `BigQuerySource`: the WeatherNext 3 BigQuery Analytics Hub listing, which
  publishes precomputed ensemble statistics (mean, p10, p25, p50, p75, p90)
  per variable rather than individual members.

The BigQuery schema used here follows the public documentation at
https://developers.google.com/weathernext/guides/bigquery : rows partitioned
by `init_time`, a `geography_polygon` per grid cell, and a repeated `forecast`
record with `time`, `hours` and `<variable>_<statistic>` columns. Precipitation
is reported in metres and converted to millimetres here.
"""

import dataclasses
import json
import os
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .config import require_env

DATA_DIR = Path(__file__).parent / "data"
SAMPLE_FORECAST = DATA_DIR / "sample_forecast.json"
SAMPLE_SITES = DATA_DIR / "sites.json"

WIND = "wind_speed_100m"
PRECIP = "total_precipitation_1hr"
VARIABLES = (WIND, PRECIP)
# Only inits from the last N days are considered when no init is pinned.
LATEST_INIT_LOOKBACK_DAYS = 2
# 6-hourly inits carry the 15-day horizon; interim hourly inits only 48 h.
SIX_HOURLY_INIT_HOURS = (0, 6, 12, 18)
# Precomputed statistics published by WeatherNext, as quantile levels.
QUANTILE_COLUMNS = {
    "p10": 0.10,
    "p25": 0.25,
    "p50": 0.50,
    "p75": 0.75,
    "p90": 0.90,
}


@dataclasses.dataclass(frozen=True)
class Site:
    """A wind farm the operations team maintains."""

    site_id: str
    name: str
    latitude: float
    longitude: float
    utc_offset_hours: float
    hub_height_m: float
    turbines: int = 0
    type: str = "onshore"


@dataclasses.dataclass
class Forecast:
    """An hourly ensemble forecast for one site.

    Exactly one of `members` or `quantiles` is set.

    Attributes:
        site_id: The site this forecast is for.
        init_time: Forecast initialization time (UTC).
        times: Valid times (UTC), one per hourly timestep.
        source: Human-readable description of where the data came from.
        members: variable -> [member][timestep] values.
        quantiles: variable -> {quantile level: [timestep] values}.
    """

    site_id: str
    init_time: datetime
    times: list[datetime]
    source: str
    members: dict[str, list[list[float]]] | None = None
    quantiles: dict[str, dict[float, list[float]]] | None = None

    @property
    def kind(self) -> str:
        return "members" if self.members is not None else "quantiles"

    def index_range(
        self, start: datetime | None, end: datetime | None
    ) -> tuple[int, int]:
        """Returns [lo, hi) timestep indices with start <= time < end."""
        lo = 0
        hi = len(self.times)
        if start is not None:
            while lo < hi and self.times[lo] < start:
                lo += 1
        if end is not None:
            while hi > lo and self.times[hi - 1] >= end:
                hi -= 1
        return lo, hi


class ForecastSource(Protocol):
    """Anything that returns a `Forecast` for a site."""

    description: str

    def get(self, site: Site) -> Forecast: ...


def parse_time(value: str) -> datetime:
    """Parses an ISO 8601 timestamp; naive values are treated as UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_sites(path: str | os.PathLike | None = None) -> dict[str, Site]:
    """Loads site metadata from SITES_JSON or the bundled sample."""
    path = Path(path or os.getenv("SITES_JSON") or SAMPLE_SITES)
    doc = json.loads(path.read_text())
    sites = [Site(**s) for s in doc["sites"]]
    return {s.site_id: s for s in sites}


class SampleSource:
    """Reads the bundled (or a user-supplied) member-level ensemble JSON."""

    def __init__(self, path: str | os.PathLike | None = None):
        path = Path(
            path or os.getenv("SAMPLE_FORECAST_JSON") or SAMPLE_FORECAST
        )
        self._doc = json.loads(path.read_text())
        self._init = parse_time(self._doc["init_time"])
        self._times = [
            self._init + timedelta(hours=h) for h in self._doc["lead_hours"]
        ]
        self.description = self._doc.get(
            "description", f"sample file {path.name}"
        )

    def get(self, site: Site) -> Forecast:
        data = self._doc["sites"].get(site.site_id)
        if data is None:
            raise KeyError(f"No sample forecast for site {site.site_id!r}")
        return Forecast(
            site_id=site.site_id,
            init_time=self._init,
            times=list(self._times),
            source=self.description,
            members={v: data[v] for v in VARIABLES},
        )


_TABLE_RE = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+){2}$")


def validate_table(table: str) -> str:
    """Checks a `project.dataset.table` id so it is safe to put in SQL."""
    if not _TABLE_RE.match(table or ""):
        raise ValueError(
            "WEATHERNEXT_BQ_TABLE must look like project.dataset.table, got"
            f" {table!r}"
        )
    return table


def _stat_columns() -> str:
    cols = [
        f"f.{v}_{s} AS {v}_{s}" for v in VARIABLES for s in QUANTILE_COLUMNS
    ]
    return ",\n  ".join(cols)


def build_latest_init_query(table: str) -> str:
    """SQL for the latest 6-hourly init in the last LATEST_INIT_LOOKBACK_DAYS."""
    hours = ", ".join(str(h) for h in SIX_HOURLY_INIT_HOURS)
    return f"""
SELECT MAX(init_time) AS init_time
FROM `{validate_table(table)}`
WHERE init_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {LATEST_INIT_LOOKBACK_DAYS} DAY)
  AND EXTRACT(HOUR FROM init_time AT TIME ZONE 'UTC') IN ({hours})
""".strip()  # noqa: S608 -- table id checked by validate_table(); values are bound parameters


def build_forecast_query(table: str) -> str:
    """SQL for one init's hourly statistics at the grid cell containing a point.

    Parameters: @init_time (TIMESTAMP), @lon, @lat (FLOAT64), @max_hours (INT64).
    """
    return f"""
SELECT
  t.init_time AS init_time,
  f.time AS time,
  f.hours AS hours,
  {_stat_columns()}
FROM `{validate_table(table)}` AS t
CROSS JOIN UNNEST(t.forecast) AS f
WHERE t.init_time = @init_time
  AND ST_INTERSECTS(t.geography_polygon, ST_GEOGPOINT(@lon, @lat))
  AND f.hours <= @max_hours
ORDER BY f.time
""".strip()  # noqa: S608 -- table id checked by validate_table(); values are bound parameters


def rows_to_forecast(
    rows: list[dict[str, Any]], site_id: str, source: str
) -> Forecast:
    """Converts BigQuery result rows into a quantile `Forecast`.

    Precipitation columns are converted from metres to millimetres.
    """
    if not rows:
        raise ValueError(f"WeatherNext returned no rows for site {site_id!r}")
    seen: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(row["time"], row)  # one grid cell; drop duplicates
    ordered = [seen[t] for t in sorted(seen)]
    quantiles: dict[str, dict[float, list[float]]] = {}
    for var in VARIABLES:
        scale = 1000.0 if var == PRECIP else 1.0
        quantiles[var] = {
            level: [float(r[f"{var}_{col}"]) * scale for r in ordered]
            for col, level in QUANTILE_COLUMNS.items()
        }
    return Forecast(
        site_id=site_id,
        init_time=ordered[0]["init_time"],
        times=[r["time"] for r in ordered],
        source=source,
        quantiles=quantiles,
    )


class BigQuerySource:
    """Reads WeatherNext 3 ensemble statistics from BigQuery.

    Requires access to the WeatherNext listing (request it with the
    WeatherNext Data Request form) and a linked dataset in your project.
    """

    def __init__(
        self,
        table: str,
        project: str | None = None,
        *,
        max_hours: int,
        timeout_seconds: float,
        init_time: str | None = None,
        client: Any = None,
    ):
        self.table = validate_table(table)
        self.max_hours = max_hours
        self.timeout_seconds = timeout_seconds
        self._init_time = parse_time(init_time) if init_time else None
        if client is None:
            from google.cloud import bigquery

            client = bigquery.Client(project=project)
        self._client = client
        self._cache: dict[str, Forecast] = {}
        self.description = (
            f"WeatherNext 3 ensemble statistics from BigQuery {table}"
        )

    def _query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        from google.cloud import bigquery

        # Bound the query both server-side (BigQuery stops the job) and
        # client-side (result() raises instead of waiting forever).
        config = bigquery.QueryJobConfig(
            query_parameters=params,
            job_timeout_ms=int(self.timeout_seconds * 1000),
        )
        job = self._client.query(sql, job_config=config)
        return [dict(r) for r in job.result(timeout=self.timeout_seconds)]

    def latest_init_time(self) -> datetime:
        if self._init_time is None:
            rows = self._query(build_latest_init_query(self.table), [])
            if not rows or rows[0]["init_time"] is None:
                raise ValueError(
                    "No WeatherNext init found in the last"
                    f" {LATEST_INIT_LOOKBACK_DAYS} days"
                )
            self._init_time = rows[0]["init_time"]
        return self._init_time

    def get(self, site: Site) -> Forecast:
        # One query per site per process: the init time is fixed once resolved.
        if site.site_id not in self._cache:
            self._cache[site.site_id] = self._fetch(site)
        return self._cache[site.site_id]

    def _fetch(self, site: Site) -> Forecast:
        from google.cloud import bigquery

        params = [
            bigquery.ScalarQueryParameter(
                "init_time", "TIMESTAMP", self.latest_init_time()
            ),
            bigquery.ScalarQueryParameter("lon", "FLOAT64", site.longitude),
            bigquery.ScalarQueryParameter("lat", "FLOAT64", site.latitude),
            bigquery.ScalarQueryParameter("max_hours", "INT64", self.max_hours),
        ]
        rows = self._query(build_forecast_query(self.table), params)
        return rows_to_forecast(rows, site.site_id, self.description)


@lru_cache(maxsize=1)
def get_source() -> ForecastSource:
    """Returns the configured forecast source (WEATHERNEXT_SOURCE)."""
    kind = (
        require_env("WEATHERNEXT_SOURCE", os.getenv("WEATHERNEXT_SOURCE"))
        .strip()
        .lower()
    )
    if kind == "sample":
        return SampleSource()
    if kind == "bigquery":
        return BigQuerySource(
            table=require_env(
                "WEATHERNEXT_BQ_TABLE", os.getenv("WEATHERNEXT_BQ_TABLE")
            ),
            project=os.getenv("WEATHERNEXT_BQ_PROJECT") or None,
            init_time=os.getenv("WEATHERNEXT_INIT_TIME") or None,
            max_hours=int(
                require_env(
                    "WEATHERNEXT_MAX_HOURS", os.getenv("WEATHERNEXT_MAX_HOURS")
                )
            ),
            timeout_seconds=float(
                require_env(
                    "WEATHERNEXT_BQ_TIMEOUT_SECONDS",
                    os.getenv("WEATHERNEXT_BQ_TIMEOUT_SECONDS"),
                )
            ),
        )
    raise ValueError(
        f"WEATHERNEXT_SOURCE must be 'sample' or 'bigquery', got {kind!r}"
    )
