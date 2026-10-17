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
"""Wind-farm maintenance window planner grounded in WeatherNext ensembles.

The agent answers operations questions such as "when can we do the blade
lift at Prairie Ridge this week, and how sure are we?". Tools load the
ensemble forecast and do all the math deterministically (see
`analysis.py`); Gemini chooses which tools to call and explains the results,
including the uncertainty, to the planner.
"""

import os
from datetime import datetime, timedelta
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from . import analysis, config
from .forecast import Forecast, get_source, load_sites, parse_time

# Candidate windows per site considered by the crew planner. More than one
# lets it fall back to a later window when a crew is busy.
PLAN_WINDOWS_PER_SITE = 10
# Retries for transient model API errors.
MODEL_RETRY_ATTEMPTS = 3


def _range(
    forecast: Forecast, start: str, horizon_hours: int
) -> tuple[datetime, datetime]:
    begin = parse_time(start) if start else forecast.init_time
    return begin, begin + timedelta(hours=horizon_hours)


def _unknown(kind: str, value: Any, known: Any) -> dict[str, str]:
    return {"error": f"Unknown {kind} {value!r}. Known: {sorted(known)}"}


def list_sites() -> dict[str, Any]:
    """Lists the wind farms, maintenance task types and the forecast source.

    Returns:
        Sites (id, name, coordinates, hub height, UTC offset, turbines),
        task types with their weather limits, the gust factor and the data
        source description.
    """
    tasks = config.load_tasks()
    return {
        "data_source": get_source().description,
        "sites": [vars(s) for s in load_sites().values()],
        "task_types": {name: vars(t) for name, t in tasks.items()},
        "gust_factor": config.gust_factor(),
        "shear_exponent": config.shear_exponent(),
    }


def get_forecast_summary(
    site_id: str, start: str = "", horizon_hours: int = 72, block_hours: int = 6
) -> dict[str, Any]:
    """Summarizes the ensemble forecast for a site in blocks of hours.

    Args:
        site_id: Site id from `list_sites`.
        start: ISO 8601 UTC start time. Empty means the forecast init time.
        horizon_hours: How many hours after `start` to cover.
        block_hours: Hours per summary block (for example 6 or 24).

    Returns:
        For each block: p10/p50/p90 of the maximum hub-height wind (m/s),
        p90 estimated gust, and p10/p50/p90 total precipitation (mm), plus the
        forecast init time and source.
    """
    sites = load_sites()
    if site_id not in sites:
        return _unknown("site", site_id, sites)
    site = sites[site_id]
    forecast = get_source().get(site)
    begin, end = _range(forecast, start, horizon_hours)
    return {
        "site_id": site_id,
        "init_time": forecast.init_time.isoformat(),
        "data_kind": forecast.kind,
        "blocks": analysis.summarize(forecast, site, begin, end, block_hours),
    }


def find_maintenance_windows(
    site_id: str,
    task_type: str,
    min_hours: int = 0,
    horizon_hours: int = 120,
    start: str = "",
    top_k: int = 5,
) -> dict[str, Any]:
    """Finds the best weather windows for a maintenance task at one site.

    Args:
        site_id: Site id from `list_sites`.
        task_type: Task type from `list_sites`, e.g. "main_component_lift".
        min_hours: Consecutive hours needed. 0 uses the task default.
        horizon_hours: How many hours after `start` to search.
        start: ISO 8601 UTC start time. Empty means the forecast init time.
        top_k: Maximum number of non-overlapping windows to return.

    Returns:
        Windows sorted by probability that every hour is within the task's
        wind, gust and precipitation limits, with local times, what makes
        unsafe members fail, and wind/precipitation percentiles.
    """
    sites = load_sites()
    tasks = config.load_tasks()
    if site_id not in sites:
        return _unknown("site", site_id, sites)
    if task_type not in tasks:
        return _unknown("task", task_type, tasks)
    site, task = sites[site_id], tasks[task_type]
    forecast = get_source().get(site)
    begin, end = _range(forecast, start, horizon_hours)
    evaluator = analysis.Evaluator(forecast, site, task)
    hours = min_hours or task.min_hours
    windows = analysis.find_windows(evaluator, begin, end, hours, top_k)
    return {
        "site_id": site_id,
        "task_type": task_type,
        "required_hours": hours,
        "limits": vars(task),
        "effective_hub_wind_limit_ms": round(
            task.effective_wind_limit(evaluator.gust_factor), 2
        ),
        "init_time": forecast.init_time.isoformat(),
        "windows": [w.to_dict(site) for w in windows],
    }


def plan_crew_schedule(
    task_type: str,
    crews: int,
    site_ids: str = "",
    horizon_hours: int = 120,
    travel_hours: float = 12.0,
    min_probability: float = -1.0,
) -> dict[str, Any]:
    """Ranks windows across sites and assigns available crews (one job per site).

    Args:
        task_type: Task type from `list_sites`.
        crews: Number of crews available.
        site_ids: Comma-separated site ids. Empty means all sites.
        horizon_hours: How many hours after the forecast init time to plan.
        travel_hours: Minimum hours between two jobs of the same crew.
        min_probability: Minimum P(safe) for a window to be scheduled. A
            negative value uses the task's go threshold.

    Returns:
        Crew assignments with window times and probabilities, sites left
        unassigned with the reason, and the expected number of completed jobs.
    """
    tasks = config.load_tasks()
    if task_type not in tasks:
        return _unknown("task", task_type, tasks)
    if crews < 1:
        return {"error": "crews must be at least 1"}
    task = tasks[task_type]
    sites = load_sites()
    wanted = [s.strip() for s in site_ids.split(",") if s.strip()] or list(
        sites
    )
    unknown = [s for s in wanted if s not in sites]
    if unknown:
        return _unknown("sites", unknown, sites)
    threshold = task.go_probability if min_probability < 0 else min_probability
    windows_by_site = {}
    init_time = None
    for sid in wanted:
        forecast = get_source().get(sites[sid])
        init_time = forecast.init_time
        begin, end = _range(forecast, "", horizon_hours)
        evaluator = analysis.Evaluator(forecast, sites[sid], task)
        windows_by_site[sid] = analysis.find_windows(
            evaluator, begin, end, top_k=PLAN_WINDOWS_PER_SITE
        )
    plan = analysis.plan_crews(windows_by_site, crews, travel_hours, threshold)
    return {
        "task_type": task_type,
        "crews": crews,
        "min_probability": threshold,
        "init_time": init_time.isoformat() if init_time else None,
        "assignments": [
            {"crew": c, **w.to_dict(sites[w.site_id])}
            for w, c in plan["assignments"]
        ],
        "unassigned_sites": plan["unassigned"],
        "expected_completed_jobs": plan["expected_completed_jobs"],
    }


INSTRUCTION = """\
You are a wind-farm operations planning assistant. You help maintenance
planners find safe weather windows for turbine work (main component crane
lifts, blade repair, nacelle service) and schedule crews, using ensemble
weather forecasts from WeatherNext.

Rules:
- Never estimate weather values or probabilities yourself. Every number you
  report must come from a tool result. If a tool returns an error, say so.
- Call `list_sites` first if you do not yet know the site ids, task types or
  limits.
- Use `find_maintenance_windows` for one site, `plan_crew_schedule` to assign
  several crews across sites, and `get_forecast_summary` for a general outlook.
- Report times in the site's local time (with the date) and say which forecast
  init time the answer is based on.
- Always state uncertainty explicitly: the probability that the whole window is
  workable, how many ensemble members support it (or, for quantile data, the
  lower bound as well), and what limits it (wind/gust or precipitation).
- Recommend a primary window and a backup when one exists. If no window meets
  the task's go threshold, say so plainly and suggest the best alternative
  (a less wind-sensitive task, a later date, or waiting for a newer forecast).
- End with one short line reminding the user that this is decision support:
  the site's lift plan, OEM limits, on-site measurements and the person in
  charge of the work have the final say.
Keep answers concise and use a small table when comparing windows or sites.
"""


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the Agent."""
    return Agent(
        name="root_agent",
        model=Gemini(
            model=os.getenv("MODEL_NAME"),
            retry_options=types.HttpRetryOptions(attempts=MODEL_RETRY_ATTEMPTS),
        ),
        description=(
            "Finds safe wind-turbine maintenance windows and schedules crews from"
            " WeatherNext ensemble forecasts, with explicit probabilities."
        ),
        instruction=INSTRUCTION,
        tools=[
            list_sites,
            get_forecast_summary,
            find_maintenance_windows,
            plan_crew_schedule,
        ],
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
