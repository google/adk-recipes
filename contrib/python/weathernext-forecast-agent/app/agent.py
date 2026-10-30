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
"""Wind-farm maintenance window planner, as an ADK 2.0 graph workflow.

Graph::

    START -> parse_request -> check_request
      check_request --plan--> load_forecasts
      check_request --retry--> parse_request        (bounded by PARSE_RETRY_LIMIT)
      check_request --reply--> reply_without_plan
      load_forecasts --ok--> find_windows -> plan_crews
      load_forecasts --error--> reply_without_plan
      plan_crews --go--> explain_plan
      plan_crews --no_go--> suggest_alternatives -> explain_no_window

Only `parse_request`, `explain_plan` and `explain_no_window` call the model.
Every number the user sees is computed by the function nodes (see
`analysis.py`); the explain agents only narrate them.
"""

import os
from datetime import datetime, timedelta
from typing import Any, Literal

from google.adk import Agent, Context, Event, Workflow
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
from pydantic import BaseModel, Field

from . import analysis, config
from .forecast import Forecast, get_source, load_sites

# Candidate windows per site the crew planner can fall back to when a crew is
# busy, and windows per site shown to the user.
PLAN_WINDOWS_PER_SITE = 10
SHOWN_WINDOWS_PER_SITE = 3
# Block length of the per-site outlook shown when no window qualifies.
OUTLOOK_BLOCK_HOURS = 24
# Retries for transient model API errors.
MODEL_RETRY_ATTEMPTS = 3
# Planning defaults used when the user does not say otherwise.
DEFAULT_CREWS = 1
DEFAULT_HORIZON_HOURS = 120

# Route names.
ROUTE_PLAN = "plan"
ROUTE_RETRY = "retry"
ROUTE_REPLY = "reply"
ROUTE_OK = "ok"
ROUTE_ERROR = "error"
ROUTE_GO = "go"
ROUTE_NO_GO = "no_go"

# Session-state keys.
_PARSE_ATTEMPTS_KEY = "parse_attempts"


class PlanRequest(BaseModel):
    """A maintenance planning request extracted from the user's message."""

    intent: Literal["plan", "help"] = Field(
        description=(
            "'plan' if the user asks when/where maintenance work can be done or"
            " how to schedule crews; 'help' for greetings, capability questions"
            " or anything else."
        )
    )
    task_type: str = Field(
        default="", description="One of the known task types, or empty."
    )
    site_ids: list[str] = Field(
        default_factory=list,
        description="Known site ids the user mentions. Empty means all sites.",
    )
    crews: int = Field(
        default=DEFAULT_CREWS, description="Number of crews available."
    )
    horizon_hours: int = Field(
        default=DEFAULT_HORIZON_HOURS,
        description="How many hours ahead to plan (this week = 120).",
    )
    min_hours: int = Field(
        default=0,
        description="Consecutive hours needed if the user says; 0 = task default.",
    )


def _parse_retry_limit() -> int:
    return int(
        config.require_env("PARSE_RETRY_LIMIT", os.getenv("PARSE_RETRY_LIMIT"))
    )


# ---------------------------------------------------------------------------
# Function nodes (deterministic, no model calls)
# ---------------------------------------------------------------------------


def check_request(node_input: dict[str, Any], ctx: Context) -> Event:
    """Validates the parsed request against known sites and tasks.

    Routes `plan` for a valid planning request, `retry` (back to the parser,
    with the error) while the retry budget lasts, and `reply` otherwise.
    """
    sites = load_sites()
    tasks = config.load_tasks()
    request = PlanRequest.model_validate(node_input or {"intent": "help"})
    problems = []
    if request.intent == "plan":
        if request.task_type not in tasks:
            problems.append(
                f"task_type {request.task_type!r} is not one of {sorted(tasks)}"
            )
        unknown = [s for s in request.site_ids if s not in sites]
        if unknown:
            problems.append(
                f"unknown site_ids {unknown}; known: {sorted(sites)}"
            )
        if request.crews < 1:
            problems.append("crews must be at least 1")
        if request.horizon_hours < 1:
            problems.append("horizon_hours must be at least 1")
        if request.min_hours < 0:
            problems.append("min_hours must be 0 or more")

    attempts = ctx.state.get(_PARSE_ATTEMPTS_KEY, 0)
    if problems and attempts < _parse_retry_limit():
        ctx.state[_PARSE_ATTEMPTS_KEY] = attempts + 1
        return Event(
            route=ROUTE_RETRY,
            output={
                "validation_errors": problems,
                "previous_parse": request.model_dump(),
            },
        )
    ctx.state[_PARSE_ATTEMPTS_KEY] = 0
    if problems or request.intent != "plan":
        return Event(
            route=ROUTE_REPLY,
            output={"intent": request.intent, "problems": problems},
        )
    plan = request.model_dump()
    plan["site_ids"] = request.site_ids or list(sites)
    return Event(route=ROUTE_PLAN, output=plan)


def reply_without_plan(node_input: dict[str, Any]) -> Event:
    """Answers help requests, invalid requests and data errors, deterministically."""
    sites = load_sites()
    tasks = config.load_tasks()
    lines = []
    for problem in node_input.get("problems", []):
        lines.append(f"I could not plan that: {problem}.")
    if node_input.get("error"):
        lines.append(f"I could not load the forecast: {node_input['error']}")
    lines += [
        "I plan wind-turbine maintenance from WeatherNext ensemble forecasts:"
        " for each window I report the probability that the whole job stays"
        " within the task's wind, gust and rain limits, and I can assign crews"
        " across sites.",
        "",
        "Sites: "
        + ", ".join(f"{s.site_id} ({s.name})" for s in sites.values())
        + ".",
        "Tasks: "
        + "; ".join(
            f"{name} (hub wind <= {t.max_wind_ms} m/s, gust <= {t.max_gust_ms}"
            f" m/s, rain <= {t.max_precip_mm} mm/h, {t.min_hours} h)"
            for name, t in tasks.items()
        )
        + ".",
        "",
        "Try: 'We have 2 crane crews this week for main component lifts. Where"
        " and when?' or 'Can we do a blade exchange at whitelee this week?'",
    ]
    text = "\n".join(lines)
    # Authored as a model turn so chat surfaces (A2A, Gemini Enterprise) show it.
    message = types.Content(role="model", parts=[types.Part(text=text)])
    return Event(message=message, output={"reply": text})


def load_forecasts(node_input: dict[str, Any], ctx: Context) -> Event:
    """Loads the ensemble forecast for every requested site.

    Routes `ok` with forecast metadata, or `error` if any site's data cannot
    be loaded (for example missing BigQuery access).
    """
    sites = load_sites()
    meta: dict[str, Any] = {}
    try:
        for sid in node_input["site_ids"]:
            forecast = get_source().get(sites[sid])
            meta[sid] = {
                "init_time": forecast.init_time.isoformat(),
                "data_kind": forecast.kind,
                "timesteps": len(forecast.times),
            }
    except Exception as e:
        return Event(
            route=ROUTE_ERROR, output={"error": f"{type(e).__name__}: {e}"}
        )
    ctx.state["plan_request"] = node_input
    return Event(
        route=ROUTE_OK,
        output={
            "request": node_input,
            "data_source": get_source().description,
            "forecasts": meta,
        },
    )


def _range(forecast: Forecast, horizon_hours: int) -> tuple[datetime, datetime]:
    # Plan from the first forecast hour (a snapshot may start after its init).
    begin = forecast.times[0]
    return begin, begin + timedelta(hours=horizon_hours)


def _site_windows(
    site_id: str, task_type: str, horizon_hours: int, min_hours: int, top_k: int
) -> list[analysis.Window]:
    site = load_sites()[site_id]
    task = config.load_tasks()[task_type]
    forecast = get_source().get(site)
    begin, end = _range(forecast, horizon_hours)
    evaluator = analysis.Evaluator(forecast, site, task)
    return analysis.find_windows(
        evaluator, begin, end, min_hours or None, top_k
    )


def find_windows(node_input: dict[str, Any]) -> Event:
    """Scores every allowed window at every requested site."""
    request = node_input["request"]
    sites = load_sites()
    task = config.load_tasks()[request["task_type"]]
    per_site = {}
    for sid in request["site_ids"]:
        windows = _site_windows(
            sid,
            request["task_type"],
            request["horizon_hours"],
            request["min_hours"],
            PLAN_WINDOWS_PER_SITE,
        )
        per_site[sid] = [w.to_dict(sites[sid]) for w in windows]
    return Event(
        output={
            **node_input,
            "task_limits": vars(task),
            "effective_hub_wind_limit_ms": round(
                task.effective_wind_limit(config.gust_factor()), 2
            ),
            "windows_by_site": per_site,
        }
    )


def plan_crews(node_input: dict[str, Any]) -> Event:
    """Assigns crews to the best windows; routes `go` or `no_go`."""
    request = node_input["request"]
    sites = load_sites()
    task = config.load_tasks()[request["task_type"]]
    windows = {
        sid: [analysis.Window.from_dict(w) for w in ws]
        for sid, ws in node_input["windows_by_site"].items()
    }
    plan = analysis.plan_crews(
        windows, request["crews"], min_probability=task.go_probability
    )
    shown = {
        sid: ws[:SHOWN_WINDOWS_PER_SITE]
        for sid, ws in node_input["windows_by_site"].items()
    }
    output = {
        "request": request,
        "data_source": node_input["data_source"],
        "forecasts": node_input["forecasts"],
        "task_limits": node_input["task_limits"],
        "effective_hub_wind_limit_ms": node_input[
            "effective_hub_wind_limit_ms"
        ],
        "go_probability": task.go_probability,
        "assignments": [
            {"crew": c, **w.to_dict(sites[w.site_id])}
            for w, c in plan["assignments"]
        ],
        "unassigned_sites": plan["unassigned"],
        "expected_completed_jobs": plan["expected_completed_jobs"],
        "best_windows_by_site": shown,
        "site_names": {sid: sites[sid].name for sid in request["site_ids"]},
    }
    route = ROUTE_GO if plan["assignments"] else ROUTE_NO_GO
    return Event(route=route, output=output)


def suggest_alternatives(node_input: dict[str, Any]) -> Event:
    """For a no-go plan, finds the best window for every other task type.

    Also adds a daily ensemble outlook per site (p10/p50/p90 hub wind and
    rain) so the explanation can say why no window qualifies.
    """
    request = node_input["request"]
    sites = load_sites()
    tasks = config.load_tasks()
    alternatives = []
    for name, task in tasks.items():
        if name == request["task_type"]:
            continue
        best = None
        for sid in request["site_ids"]:
            ws = _site_windows(sid, name, request["horizon_hours"], 0, 1)
            if ws and (
                best is None
                or ws[0].probability_safe > best[0].probability_safe
            ):
                best = (ws[0], sid)
        if best is not None:
            alternatives.append(
                {
                    "task_type": name,
                    "go_probability": task.go_probability,
                    **best[0].to_dict(sites[best[1]]),
                }
            )
    outlook = {}
    for sid in request["site_ids"]:
        forecast = get_source().get(sites[sid])
        begin, end = _range(forecast, request["horizon_hours"])
        outlook[sid] = analysis.summarize(
            forecast, sites[sid], begin, end, block_hours=OUTLOOK_BLOCK_HOURS
        )
    return Event(
        output={
            **node_input,
            "alternative_tasks": alternatives,
            "daily_outlook_by_site": outlook,
        }
    )


# ---------------------------------------------------------------------------
# LLM nodes
# ---------------------------------------------------------------------------


def _model() -> Gemini:
    return Gemini(
        model=os.getenv("MODEL_NAME"),
        retry_options=types.HttpRetryOptions(attempts=MODEL_RETRY_ATTEMPTS),
    )


def _parse_instruction(_: ReadonlyContext) -> str:
    sites = load_sites()
    tasks = config.load_tasks()
    site_lines = "\n".join(f"- {s.site_id}: {s.name}" for s in sites.values())
    task_lines = "\n".join(f"- {n}: {t.description}" for n, t in tasks.items())
    return f"""\
You turn a wind-farm operations planner's message into a structured planning
request. Do not answer the question yourself.

Known sites:
{site_lines}

Known task types:
{task_lines}

Map the user's words to the known ids (for example "blade exchange" or "crane
lift" -> main_component_lift; "leading-edge repair" -> blade_repair; "service
visit" or "climb" -> nacelle_service; "Whitelee" -> whitelee). Use
earlier turns of the conversation for follow-up questions ("what about blade
repair instead?"). Leave site_ids empty when the user means all sites. "This
week" means 120 hours; "next 3 days" means 72. Use intent "help" for
greetings or questions about what you can do.

If the input contains validation_errors, fix exactly those fields using the
known ids above and the user's original message.
"""


parse_request = Agent(
    name="parse_request",
    model=_model(),
    mode="single_turn",
    description="Extracts a structured maintenance planning request.",
    instruction=_parse_instruction,
    output_schema=PlanRequest,
)

_EXPLAIN_RULES = """\
Rules:
- Use only numbers that appear in the JSON. Never estimate weather values or
  probabilities yourself.
- Give times in site local time with the weekday and date (start_local /
  end_local), and name the forecast init time and data source.
- For every recommended window state P(safe) as a percentage, the members in
  support (members_safe / members_total) or, for quantile data, also
  probability_safe_lower_bound, and the main risk (fail_fraction_wind_or_gust
  vs fail_fraction_precipitation).
- Keep it short: one small markdown table, then 2-4 bullet points.
- End with one line: this is decision support; the site's lift plan, OEM
  limits, on-site measurements and the person in charge of the work have the
  final say.
"""

explain_plan = Agent(
    name="explain_plan",
    model=_model(),
    mode="single_turn",
    include_contents="none",
    description="Explains a crew plan computed by the planner.",
    instruction=f"""\
You are a wind-farm operations planning assistant. The input is a JSON crew
plan computed deterministically from a WeatherNext ensemble forecast. Present
it to the planner: the assignment for each crew (site, window, probability),
a backup window from best_windows_by_site where one exists, and which sites
were left unassigned and why.

{_EXPLAIN_RULES}""",
)

explain_no_window = Agent(
    name="explain_no_window",
    model=_model(),
    mode="single_turn",
    include_contents="none",
    description="Explains why no window meets the go threshold.",
    instruction=f"""\
You are a wind-farm operations planning assistant. The input is a JSON result
in which NO window meets the task's go threshold (go_probability). Say that
plainly first. Then show the best window found per site from
best_windows_by_site (with its probability), the alternatives in
alternative_tasks (a less wind-sensitive task that could use the crew), and in
one sentence what daily_outlook_by_site shows (for example p50 hub wind above
the effective limit). Suggest waiting for a newer forecast run where relevant. Do not recommend going ahead
with the original task.

{_EXPLAIN_RULES}""",
)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def create_workflow() -> Workflow:
    """Builds the planning workflow graph."""
    return Workflow(
        name="maintenance_planner",
        description=(
            "Finds safe wind-turbine maintenance windows and schedules crews from"
            " WeatherNext ensemble forecasts, with explicit probabilities."
        ),
        edges=[
            ("START", parse_request, check_request),
            (
                check_request,
                {
                    ROUTE_PLAN: load_forecasts,
                    ROUTE_RETRY: parse_request,
                    ROUTE_REPLY: reply_without_plan,
                },
            ),
            (
                load_forecasts,
                {ROUTE_OK: find_windows, ROUTE_ERROR: reply_without_plan},
            ),
            (find_windows, plan_crews),
            (
                plan_crews,
                {ROUTE_GO: explain_plan, ROUTE_NO_GO: suggest_alternatives},
            ),
            (suggest_alternatives, explain_no_window),
        ],
    )


root_agent = create_workflow()

app = App(
    root_agent=root_agent,
    name="app",
)
