# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Roll-your-own FastAPI dashboard for the AQA agent.

Wiring::

    ui/web bundle (browser)
      │
      ├─ /, any other path ──► serve_dashboard_shell(), serve_spa() ──► static_v2 files
      │
      ├─ /api/* ──► _get_backend_client() ──► AgentRuntimeClient | AdkHttpClient
      │               build_client_from_env()             │ post_command()
      │                                                   ▼
      │                                     fast_api_app: core.command_routes
      │
      ├─ /a2a, agent card ──► proxy_a2a_request() ──► fast_api_app: chat A2A route
      │     _resolve_a2a_upstream() ──► resolve_a2a_base_url()
      │     get_adc_token(), _forward(), _stream_a2a()
      │
      ├─ /lha/contexts ──► task_reader ──► GCS
      │     list_contexts(), list_context_tasks(), delete_context()
      │
      └─ other /lha/*, /feedback ──► _build_lha_unavailable_response()

This is the *leanest* option: it reuses the exact web stack `adk web` already
ships (FastAPI + Starlette + uvicorn) and the shared, SDK-agnostic client -- so
it talks to either:

  * a real **Agent Runtime** in Gemini platform
    (``AGENT_ENGINE_RESOURCE_ID``) via HTTP command routes -- the default, or
  * a **local** ``fast_api_app`` served by ``tools/local_ui.sh``
    (``AGENT_ADK_BASE_URL`` + ``AQA_BACKEND=adk``) via HTTP command routes.

The browser front-end is the React app in ``ui/web``, built to a ``static_v2``
bundle this module serves. Every dashboard action is an HTTP command route
rather than a chat turn, so the UI never depends on the LLM to route a click.

Endpoints
---------
GET  /                          -> the dashboard shell (static_v2/index.html)
GET  /api/config                -> {config: {...}} -- the effective configuration
GET  /api/investigations        -> {runs: [...]} for the investigations table
POST /api/investigations        -> start a run -> {run: {...}} | {ok: true} | {error}
GET  /api/stats                 -> {stats: {...}} totals across every run
GET  /api/investigations/{id}   -> {run: {...}} for one run
GET  /api/daily                 -> {days: [...]} per-day figures for the charts
GET  /api/insights              -> {insights: [...], total, next_page_token}
GET  /api/insights/{id}         -> {insight: {...}, occurrences: [...], next_page_token}
POST /api/insights/{id}/dismiss -> the agent's reply -- hide it from the list
POST /api/insights/{id}/merge   -> the agent's reply -- record it as a duplicate
GET  /api/health                -> {verdict, reason, ...} -- is the loop turning
GET  /api/source                -> {available, reason} -- can findings cite code
GET  /api/investigations/{id}/cases/{case} -> {case: {...}} -- one conversation
GET  /healthz                   -> liveness probe

``/`` serves the dashboard: ``/assets/*`` and any other file in the bundle are
served beside it, every unclaimed path falls through to the shell so the
client-side router can match it, and ``/v2/*`` permanently redirects to ``/*``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import task_reader
from ambient_quality_shared.agent_client import (
    build_client_from_env,
    get_adc_token,
    resolve_a2a_base_url,
)
from ambient_quality_shared.protocol import (
    CHAT_A2A_APP,
    CONFIG_ROUTE,
    GOAL_GET_ROUTE,
    GOAL_SET_ROUTE,
    GOAL_VERSIONS_ROUTE,
    HEALTH_ROUTE,
    INSIGHTS_DISMISS_ROUTE,
    INSIGHTS_GET_ROUTE,
    INSIGHTS_LIST_ROUTE,
    INSIGHTS_MERGE_ROUTE,
    INVESTIGATIONS_DAILY_ROUTE,
    INVESTIGATIONS_GET_ROUTE,
    INVESTIGATIONS_LIST_ROUTE,
    INVESTIGATIONS_SCHEDULE_ROUTE,
    INVESTIGATIONS_STATS_ROUTE,
    MEMORIES_DELETE_ROUTE,
    MEMORIES_LIST_ROUTE,
    SOURCE_ROUTE,
    TRAJECTORIES_CASE_ROUTE,
)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))

# The front end (ui/web, built to a `static_v2` bundle). The image copies it in
# beside this file, and the ui/web build writes it there in a checkout;
# `AQA_STATIC_V2_DIR` overrides the location.
STATIC_V2_DIR = os.environ.get("AQA_STATIC_V2_DIR") or os.path.join(
    HERE, "static_v2"
)
V2_INDEX = os.path.join(STATIC_V2_DIR, "index.html")

# Whether there is a front end to serve at all. There is nothing to fall back
# to, so this decides between the dashboard and a page that says how to build
# one -- and the API keeps answering either way, which is why a missing bundle
# is not an import error. An image without one is misbuilt; a checkout has none
# until `npm run build` has been run once.
BUNDLE_AVAILABLE = os.path.isfile(V2_INDEX)

_NO_BUNDLE = (
    "No built front end at {dir}. Build it with:\n"
    "    npm --prefix ui/web ci && npm --prefix ui/web run build\n"
    "(the image builds it itself, in the Dockerfile's Node stage)."
)

if not BUNDLE_AVAILABLE:
    logging.getLogger(__name__).warning(_NO_BUNDLE.format(dir=STATIC_V2_DIR))

app = FastAPI(title="AQA FastAPI dashboard")


# --------------------------------------------------------------------------- #
# The target comes from the environment, which is fixed for the process, so
# one client serves every request.
# --------------------------------------------------------------------------- #
@functools.cache
def _get_backend_client() -> Any:
    """Returns the backend client for a deployed engine or a local agent server.

    Returns:
        Cached backend client instance (`AgentRuntimeClient` or `AdkHttpClient`).
    """
    return build_client_from_env()


def _extract_window_args(request: Request) -> dict[str, str]:
    """Reads telemetry time window parameters from the request query.

    Normalizes query parameter names into agent arguments. Absent parameters
    are omitted so backend tools apply their internal defaults.

    Args:
        request: Incoming HTTP request.

    Returns:
        Mapping containing non-empty `window_start` and `window_end` bounds.
    """
    params = request.query_params
    window = {
        "window_start": params.get("windowStart") or "",
        "window_end": params.get("windowEnd") or "",
    }
    return {k: v for k, v in window.items() if v}


def _encode_tri_state(value: str | None) -> str:
    """Convert a tri-state query parameter into the backend filter sentinel.

    Args:
        value: The raw query parameter string, or ``None`` if omitted.

    Returns:
        ``"true"``, ``"false"``, or ``""`` for no preference.

    Uses strings rather than booleans so that ``False`` is not discarded
    when forwarding routes strip falsy parameters.

    Duplicated by ``core.command_routes._encode_tri_state`` and
    ``tools.orchestrator.insight_tools._parse_tri_state``: the UI image and the
    agent ship as separate deployables with no shared import path, so each end
    of the sentinel contract carries its own copy. Change all three together.
    """
    if value in ("1", "true", "True"):
        return "true"
    if value in ("0", "false", "False"):
        return "false"
    return ""


def _parse_count(value: str | None) -> int:
    """Parses an integer count parameter, returning 0 when absent or invalid.

    Zero represents no preference in forwarding routes, allowing tools to
    apply default pagination limits.

    Args:
        value: Raw count query parameter string, or None.

    Returns:
        Parsed non-negative integer, or 0 if omitted or invalid.
    """
    try:
        return int(value or 0)
    except ValueError:
        return 0


@app.get("/")
async def serve_dashboard_shell() -> Response:
    """Serves the dashboard shell index.html or build instructions if absent.

    Returns 503 rather than 404 when the bundle is missing to guide resolution,
    while keeping API routes accessible.

    Returns:
        FileResponse serving the static index page, or PlainTextResponse on 503.
    """
    if BUNDLE_AVAILABLE:
        return FileResponse(V2_INDEX)
    return PlainTextResponse(
        _NO_BUNDLE.format(dir=STATIC_V2_DIR), status_code=503
    )


# --------------------------------------------------------------------------- #
# Dashboard API.
#
# Routes catch failures into an ``{"error": ...}`` body (``/api/source`` into
# ``{available, reason}``), which mutation routes pair with a 400/404/502 status,
# so the UI degrades gracefully instead of surfacing a raw 5xx. Route ordering is irrelevant: FastAPI path
# params don't span ``/``, so ``/{run_id}`` and its siblings never collide.
# --------------------------------------------------------------------------- #

_NOT_DEPLOYED = (
    "AQuA's agent is not serving its API yet. The Agent Runtime agent is still "
    "running the placeholder image Terraform creates it with, or is rolling out "
    "a new revision; either takes several minutes. Retry shortly, and run "
    "`agents-cli deploy` if no deploy has finished against this engine."
)
"""What a panel says when nothing behind the engine serves the routes.

Terraform creates the engine with a hello-world placeholder image, which
answers every command route with a body that is not JSON until `agents-cli
deploy` replaces it. The dashboard draws this amber rather than red by matching
its first sentence -- see `NOT_DEPLOYED_RE` in `ui/web/lib/failure.ts`.
"""


def _is_not_deployed(exc: BaseException) -> bool:
    """Checks whether an exception indicates the backend service is not deployed.

    Identifies non-JSON response bodies or gateway errors (502/503/504) during
    container revisions. Excludes 404 errors, which indicate version skew.

    Args:
        exc: Exception raised during backend communication.

    Returns:
        True if the exception indicates an undeployed or restarting backend.
    """
    if isinstance(exc, json.JSONDecodeError):
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (502, 503, 504)


def _format_error(exc: BaseException) -> str:
    """Formats an exception into a user-facing error message.

    Args:
        exc: Exception caught during command execution.

    Returns:
        Descriptive single-line error message string.
    """
    if _is_not_deployed(exc):
        return _NOT_DEPLOYED
    return f"{type(exc).__name__}: {exc}"


@app.get("/api/config")
async def get_effective_config(request: Request) -> dict:
    """Fetches the effective runtime configuration.

    Args:
        request: Incoming HTTP request.

    Returns:
        Dictionary containing the configuration object or an error message.
    """
    try:
        result = await _get_backend_client().post_command(CONFIG_ROUTE)
    except Exception as exc:
        return {"error": _format_error(exc)}
    config = (result or {}).get("config")
    if not isinstance(config, dict):
        return {"error": "The agent returned no configuration."}
    return {"config": config}


@app.get("/api/health")
async def get_ambient_health(request: Request) -> dict:
    """Fetches the ambient loop execution health verdict.

    Reports empty payloads as errors so the UI badge does not misinterpret
    missing verdicts as healthy operation.

    Args:
        request: Incoming HTTP request.

    Returns:
        Dictionary containing the health verdict or an error message.
    """
    try:
        result = await _get_backend_client().post_command(HEALTH_ROUTE)
    except Exception as exc:
        return {"error": _format_error(exc)}
    if not result:
        return {"error": "The agent returned no health verdict."}
    return result


@app.get("/api/source")
async def get_source_snapshot(request: Request) -> dict:
    """Checks whether the observed agent's source code is available for citation.

    Args:
        request: Incoming HTTP request.

    Returns:
        Dictionary indicating source availability and status details.
    """
    try:
        result = await _get_backend_client().post_command(SOURCE_ROUTE)
    except Exception as exc:
        return {"available": False, "reason": _format_error(exc)}
    return result or {
        "available": False,
        "reason": "The agent returned nothing.",
    }


@app.get("/api/investigations/{run_id}/cases/{case_id}")
async def get_case_conversation(
    run_id: str, case_id: str, request: Request
) -> dict:
    """Retrieves a full conversation trace for a specific evaluation case.

    Fetches the observed agent's user session rather than investigation run steps.

    Args:
        run_id: Investigation run identifier.
        case_id: Evaluated conversation trajectory identifier.
        request: Incoming HTTP request.

    Returns:
        Dictionary containing the case conversation or an error message.
    """
    try:
        result = await _get_backend_client().post_command(
            TRAJECTORIES_CASE_ROUTE,
            {"trajectory_id": case_id, "run_id": run_id},
        )
    except Exception as exc:
        return {"error": _format_error(exc)}
    if not result:
        return {"error": f"No conversation returned for case {case_id!r}."}
    if result.get("error"):
        return {"error": result["error"]}
    return result


@app.get("/api/investigations")
async def list_investigations(request: Request) -> dict:
    """Lists investigation runs matching the optional time window.

    Forwards backend errors so storage read failures are not rendered as
    empty run history.

    Args:
        request: Incoming HTTP request containing optional time window parameters.

    Returns:
        Dictionary containing investigation runs and optional error message.
    """
    try:
        result = await _get_backend_client().post_command(
            INVESTIGATIONS_LIST_ROUTE, _extract_window_args(request) or None
        )
    except Exception as exc:
        return {"runs": [], "error": _format_error(exc)}
    result = result or {}
    runs = result.get("runs") or []
    if result.get("error"):
        return {"runs": runs, "error": result["error"]}
    return {"runs": runs}


@app.post("/api/investigations")
async def start_investigation(request: Request) -> dict:
    """Starts an investigation run.

    Schedules the execution job and returns the new run record or confirmation.
    Prioritizes error reporting over run IDs if job submission fails.

    Args:
        request: Incoming HTTP request.

    Returns:
        Dictionary containing the scheduled run details, success confirmation, or error.
    """
    try:
        result = await _get_backend_client().post_command(
            INVESTIGATIONS_SCHEDULE_ROUTE
        )
    except Exception as exc:
        return {"error": _format_error(exc)}
    if result and result.get("error"):
        return {"error": result["error"]}
    if result and result.get("run_id"):
        return {"run": result}
    return {"ok": True}


@app.get("/api/stats")
async def get_investigation_stats(request: Request) -> dict:
    """Aggregates counter totals across investigation runs in the time window.

    Args:
        request: Incoming HTTP request with optional window boundaries.

    Returns:
        Dictionary with aggregated stats, window metadata, or error details.
    """
    try:
        result = await _get_backend_client().post_command(
            INVESTIGATIONS_STATS_ROUTE, _extract_window_args(request) or None
        )
    except Exception as exc:
        return {"stats": {}, "window": {}, "error": _format_error(exc)}
    result = result or {}
    stats = result.get("stats") or {}
    window = result.get("window") or {}
    if result.get("error"):
        return {"stats": stats, "window": window, "error": result["error"]}
    return {"stats": stats, "window": window}


@app.get("/api/investigations/{run_id}")
async def get_investigation_record(run_id: str, request: Request) -> dict:
    """Retrieves an investigation run record with full event history.

    Args:
        run_id: Identifier of the investigation run.
        request: Incoming HTTP request.

    Returns:
        Dictionary containing the investigation run record or error message.
    """
    try:
        result = await _get_backend_client().post_command(
            INVESTIGATIONS_GET_ROUTE, {"run_id": run_id}
        )
    except Exception as exc:
        return {"error": _format_error(exc)}
    if not result:
        return {"error": f"No record returned for run {run_id!r}."}
    if result.get("error") and not result.get("run_id"):
        return {"error": result["error"]}
    return {"run": result}


@app.get("/api/daily")
async def get_daily_trends(request: Request) -> dict:
    """Retrieves daily aggregated metric trends for trend charts.

    Days without sweeps are omitted rather than zeroed, distinguishing inactivity
    from zero detected issues.

    Args:
        request: Incoming HTTP request containing optional time window parameters.

    Returns:
        Dictionary containing the daily trend list or error details.
    """
    try:
        result = await _get_backend_client().post_command(
            INVESTIGATIONS_DAILY_ROUTE, _extract_window_args(request) or None
        )
    except Exception as exc:
        return {"days": [], "error": _format_error(exc)}
    result = result or {}
    days = result.get("days") or []
    if result.get("error"):
        return {"days": days, "error": result["error"]}
    return {"days": days}


@app.get("/api/insights")
async def list_insights(request: Request) -> dict:
    """Lists grouped defect insights matching filter and pagination criteria.

    Args:
        request: Incoming HTTP request with optional query filters (status,
            runId, hasRootCause, pageToken, orderBy, pageSize, day, window).

    Returns:
        Dictionary containing formatted insights, total count, affected
        conversation counts, and next page token.
    """
    params = request.query_params
    args = {
        "status": params.get("status") or "",
        "run_id": params.get("runId") or "",
        "has_root_cause": _encode_tri_state(params.get("hasRootCause")),
        "page_token": params.get("pageToken") or "",
        "order_by": params.get("orderBy") or "",
        "page_size": _parse_count(params.get("pageSize")),
        "day": params.get("day") or "",
        **_extract_window_args(request),
    }
    args = {key: value for key, value in args.items() if value}
    try:
        result = await _get_backend_client().post_command(
            INSIGHTS_LIST_ROUTE, args
        )
    except Exception as exc:
        return {"insights": [], "error": _format_error(exc)}
    if not result:
        return {
            "insights": [],
            "error": "The agent returned no insights payload.",
        }
    if result.get("error"):
        return {"insights": [], "error": result["error"]}
    return {
        "insights": [
            _format_insight_for_v2(i) for i in result.get("insights") or []
        ],
        "total": result.get("total", 0),
        # Passed through as the route aggregated it, over the whole filtered
        # set. The header renders this instead of summing the page it was
        # handed, which states a fraction of the deployment as the total and
        # counts one conversation once per sweep and once per insight.
        "conversations": result.get("conversations"),
        "next_page_token": result.get("next_page_token"),
    }


# --------------------------------------------------------------------------- #
# The v2 dashboard's vocabulary
#
# The replacement dashboard was built against a schema whose insight records
# carry root causes, confidence metrics, and verification source lines.
# This deployment records root causes through `record_root_cause` in the chat
# agent during on-demand triage rather than during investigation sweeps.
#
# Vocabulary reconciliation lives here in the dashboard service because the
# underlying command routes serve CLI and agent clients that do not require
# front-end specific fields.
#
# **Nothing here invents a value.** Where we hold a field under another name
# it is projected; where we do not hold it, the key is left absent and the
# client decides how the gap reads. `confidence` is not measured by this
# pipeline, so the dashboard does not render it.
# --------------------------------------------------------------------------- #

VERIFICATION_STEP = "verification"
"""Key under which a sighting's cluster-verification result is stored in
`analyses`. A hand-copy of the agent's `VERIFICATION_ANALYSIS_KEY`, because
this image ships without the agent's packages; `tests/test_ui_investigations.py`
holds the two level."""


def _extract_verification_field(analyses: Any, name: str) -> str:
    """Extracts a string field from cluster-verification analyses.

    Args:
        analyses: Analyses dictionary or object from a sighting.
        name: Key name inside the verification step to retrieve.

    Returns:
        Stripped string value, or an empty string if absent or invalid.
    """
    if not isinstance(analyses, dict):
        return ""
    step = analyses.get(VERIFICATION_STEP)
    if not isinstance(step, dict):
        return ""
    value = step.get(name)
    return value.strip() if isinstance(value, str) else ""


def _extract_diagnosis(analyses: Any) -> str:
    """Extracts the verification explanation from the sighting analyses map.

    Args:
        analyses: Analyses mapping from an occurrence.

    Returns:
        Explanation string, or an empty string if missing.
    """
    return _extract_verification_field(analyses, "explanation")


def _extract_refined_label(analyses: Any) -> str:
    """Extracts the refined label proposed during verification.

    The original `Insight.label` is preserved as the recurrence key for later
    sweeps; this value serves purely as a display name.

    Args:
        analyses: Analyses mapping from an occurrence.

    Returns:
        Refined label string, or an empty string if missing.
    """
    return _extract_verification_field(analyses, "refined_label")


def _format_insight_for_v2(
    insight: dict,
    occurrences: list[dict] | None = None,
    root_causes: list[dict] | None = None,
) -> dict:
    """Formats an insight record for the dashboard interface.

    1. Derives `impact` from the total affected trace count across sightings.
    2. Extracts `diagnosis` from the most recent sighting's verification analysis.
    3. Computes `has_root_cause` to allow summary badges without full record fetches.
       Investigation sweeps do not record root causes during detection; root causes
       are recorded on demand by `record_root_cause` during interactive triage.
       Undiagnosed insights represent active findings awaiting diagnosis.
    4. Applies `refined_label` from verification analysis when available.

    Args:
        insight: Raw insight record from the backend store.
        occurrences: Optional list of occurrence records for this insight.
        root_causes: Optional list of root cause analysis records.

    Returns:
        Formatted insight dictionary with impact, diagnosis, and status markers.
    """
    diagnosis = insight.get("diagnosis") or ""
    if not diagnosis and occurrences:
        diagnosis = str(occurrences[0].get("diagnosis") or "")
    diagnosed = (
        bool(insight.get("has_root_cause"))
        or bool(root_causes)
        or any(
            occurrence.get("root_causes") for occurrence in occurrences or []
        )
    )
    refined_label = insight.get("refined_label") or ""
    if not refined_label and occurrences:
        refined_label = str(occurrences[0].get("refined_label") or "")
    return {
        **insight,
        "impact": insight.get("impact", insight.get("trace_count", 0)),
        "diagnosis": diagnosis,
        "has_root_cause": diagnosed,
        "refined_label": refined_label,
    }


def _format_occurrence_for_v2(occurrence: dict) -> dict:
    """Formats a sighting occurrence for the dashboard interface.

    1. Projects `trajectory_ids` to `evidence_case_ids`.
    2. Maps `trace_count` to `cases_checked`.
    3. Populates Cloud Trace console links keyed by trajectory ID.
    4. Extracts `diagnosis` and `refined_label` from verification analyses without
       conflating autorater evaluations with human or agent diagnoses.

    Args:
        occurrence: Raw occurrence record from the backend store.

    Returns:
        Formatted occurrence dictionary for dashboard display.
    """
    view = dict(occurrence)
    view.setdefault("evidence_case_ids", occurrence.get("trajectory_ids") or [])
    view.setdefault("cases_checked", int(occurrence.get("trace_count") or 0))
    view.setdefault("console_urls", _extract_console_urls(occurrence))
    view["root_causes"] = occurrence.get("root_causes") or []
    if not view.get("diagnosis"):
        view["diagnosis"] = _extract_diagnosis(occurrence.get("analyses"))
    if not view.get("refined_label"):
        view["refined_label"] = _extract_refined_label(
            occurrence.get("analyses")
        )
    return view


def _extract_console_urls(occurrence: dict) -> dict[str, str]:
    """Maps conversation trajectory IDs to their Cloud Trace console URLs.

    Returns a dictionary keyed by trajectory ID so caller-defined iteration order
    in `evidence_case_ids` is preserved.

    Args:
        occurrence: Sighting occurrence record containing trajectory metadata.

    Returns:
        Dictionary mapping trajectory IDs to Cloud Trace console URLs.
    """
    return {
        trajectory["trajectory_id"]: trajectory["console_url"]
        for trajectory in occurrence.get("trajectories") or []
        if trajectory.get("trajectory_id") and trajectory.get("console_url")
    }


@app.get("/api/insights/{insight_id}")
async def get_insight_detail(insight_id: str, request: Request) -> dict:
    """Retrieves detailed insight information including occurrences and root causes.

    Heavy conversation trace payloads are omitted by default unless requested
    via `includeTraces=1`.

    Args:
        insight_id: Unique insight identifier.
        request: Incoming HTTP request containing optional query parameters
            (`runId`, `pageToken`, `includeTraces`).

    Returns:
        Dictionary containing the formatted insight, occurrences, root causes,
        and next page token.
    """
    params = request.query_params
    args: dict[str, Any] = {
        "insight_id": insight_id,
        "run_id": params.get("runId") or "",
        "page_token": params.get("pageToken") or "",
        "include_traces": params.get("includeTraces") in ("1", "true"),
        # The links, without the conversations they link to. The panel shows
        # each failing rubric's expected/actual behavior and offers a way out
        # to the trace; the rubric's own conversation copy is the heavy payload
        # a harness wants, and stays behind `includeTraces`.
        "include_trajectories": True,
    }
    try:
        result = await _get_backend_client().post_command(
            INSIGHTS_GET_ROUTE, args
        )
    except Exception as exc:
        return {"error": _format_error(exc)}
    if not result:
        return {"error": f"No record returned for insight {insight_id!r}."}
    if result.get("error"):
        return {"error": result["error"]}
    occurrences = [
        _format_occurrence_for_v2(o) for o in result.get("occurrences") or []
    ]
    root_causes = result.get("root_causes") or []
    return {
        # Occurrences first: the insight's diagnosis is the newest sighting's.
        "insight": _format_insight_for_v2(
            result.get("insight") or {}, occurrences, root_causes
        ),
        "occurrences": occurrences,
        # Top-level root causes ensure diagnoses remain accessible even if an
        # occurrence ID was regenerated during a retry.
        "root_causes": root_causes,
        "next_page_token": result.get("next_page_token"),
    }


# --------------------------------------------------------------------------- #
# The two judgements an operator makes on the insight list.
#
# Both take the id in the path and answer a one-key object, because that is what
# the front end was written against and it is not ours to choose. Dismiss sends
# no body at all -- not an empty one -- so nothing here may read it.
#
# Neither hides anything client-side: both mutations just re-read
# `/api/insights`, so it is the agent's list query that has to stop returning
# the row. A 4xx here is the button reading as broken, which is the state this
# pair of routes exists to end.
# --------------------------------------------------------------------------- #


@app.post("/api/insights/{insight_id}/dismiss")
async def dismiss_insight(insight_id: str) -> Any:
    """Dismisses an insight, hiding it from default active listings.

    Args:
        insight_id: Identifier of the insight to dismiss.

    Returns:
        JSON response with the backend result or HTTP error status.
    """
    try:
        result = await _get_backend_client().post_command(
            INSIGHTS_DISMISS_ROUTE, {"insight_id": insight_id}
        )
    except Exception as exc:
        return JSONResponse({"error": _format_error(exc)}, status_code=502)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, status_code=404)
    return result


@app.post("/api/insights/{insight_id}/merge")
async def merge_insight(insight_id: str, request: Request) -> Any:
    """Merges an insight into a target insight as a duplicate.

    Args:
        insight_id: Identifier of the source insight being merged.
        request: Incoming HTTP request containing `target_insight_id` in JSON body.

    Returns:
        JSON response confirming the merge or returning an HTTP error.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)
    target = str((body or {}).get("target_insight_id") or "")
    if not target:
        return JSONResponse(
            {"error": "target_insight_id is required."}, status_code=400
        )
    try:
        result = await _get_backend_client().post_command(
            INSIGHTS_MERGE_ROUTE,
            {"insight_id": insight_id, "target_insight_id": target},
        )
    except Exception as exc:
        return JSONResponse({"error": _format_error(exc)}, status_code=502)
    if result.get("error"):
        # Both refusals are about which insights were named, so they read as the
        # caller's mistake rather than this service's.
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


@app.get("/healthz")
async def get_liveness() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# A2A proxy for the dashboard's chat agent.
#
# The browser holds no ADC and this service does not share an origin with the
# deployed engine, so a JSON-RPC call from the dashboard's A2A client has to
# land here first and be forwarded with a bearer token. Everything the browser
# sees is same-origin: it hardcodes `/a2a` for RPC and reads the agent card
# from the well-known root, which is why both aliases below exist.
# --------------------------------------------------------------------------- #

AGENT_CARD_SUFFIX = "/.well-known/agent-card.json"


def _resolve_a2a_upstream() -> tuple[str, bool]:
    """Resolves the upstream base URL and authentication requirement for A2A proxying.

    Returns:
        Tuple of `(base_url, needs_auth)` where `needs_auth` indicates if bearer
        tokens must be attached.

    Raises:
        RuntimeError: If no supported backend client is configured.
    """
    return resolve_a2a_base_url(_get_backend_client())


async def _forward(
    method: str, url: str, *, headers: dict[str, str], content: bytes | None
) -> Any:
    """Executes a non-streaming outbound HTTP request to the upstream backend.

    Args:
        method: HTTP method (e.g. GET, POST).
        url: Destination upstream URL.
        headers: Request HTTP headers.
        content: Raw request payload bytes, or None.

    Returns:
        httpx.Response from the upstream server.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
        return await http.request(method, url, content=content, headers=headers)


def _create_streaming_client() -> Any:
    """Creates an HTTP client with a generous timeout for streaming A2A responses.

    Returns:
        Configured `httpx.AsyncClient` instance.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(300.0))


def _build_json_error(status_code: int, message: str) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )


@app.get(AGENT_CARD_SUFFIX)
async def proxy_a2a_agent_card(request: Request) -> Any:
    """Proxies the A2A agent card at the well-known discovery path.

    Args:
        request: Incoming HTTP request.

    Returns:
        Response containing the rewritten agent card JSON.
    """
    return await proxy_a2a_request(
        f"{CHAT_A2A_APP}{AGENT_CARD_SUFFIX}", request
    )


@app.post("/a2a")
async def proxy_a2a_rpc(request: Request) -> Any:
    """Proxies the chat agent JSON-RPC endpoint.

    Args:
        request: Incoming HTTP request.

    Returns:
        Proxied JSON-RPC response or streaming SSE response.
    """
    return await proxy_a2a_request(CHAT_A2A_APP, request)


@app.api_route("/a2a/{path:path}", methods=["GET", "POST"])
async def proxy_a2a_request(path: str, request: Request) -> Any:
    """Forwards an A2A agent card or JSON-RPC request to the upstream backend.

    Args:
        path: Target subpath under the A2A application route.
        request: Incoming HTTP request.

    Returns:
        Proxied Response or StreamingResponse.
    """
    try:
        base, needs_auth = _resolve_a2a_upstream()
    except RuntimeError as exc:
        return _build_json_error(503, str(exc))

    headers = {
        "content-type": request.headers.get("content-type", "application/json")
    }
    if needs_auth:
        headers["authorization"] = f"Bearer {get_adc_token()}"

    url = f"{base}/a2a/{path}"
    body = await request.body()
    if _is_streaming_send(body):
        return StreamingResponse(
            _stream_a2a(url, headers, body),
            media_type="text/event-stream",
            # Buffering a stream defeats the point of relaying it frame by
            # frame, and an intermediary will do it unless told not to.
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    upstream = await _forward(
        request.method, url, headers=headers, content=body or None
    )
    if upstream.status_code < 400 and not _carries_json(upstream):
        # A2A speaks JSON; a success that is not JSON is the placeholder's own
        # page, which the browser's A2A client can only fail to parse.
        return _build_json_error(503, _NOT_DEPLOYED)
    content = upstream.content
    if path.endswith(AGENT_CARD_SUFFIX) and upstream.status_code == 200:
        content = _rewrite_card_to_same_origin(content, base)
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def _carries_json(response: Any) -> bool:
    """Checks whether an HTTP response carries a valid JSON body.

    Args:
        response: Upstream HTTP response object.

    Returns:
        True if the response content type or body is valid JSON.
    """
    if not response.content:
        return False
    media = (
        (response.headers.get("content-type") or "")
        .split(";")[0]
        .strip()
        .lower()
    )
    if media.endswith("json"):
        return True
    try:
        json.loads(response.content)
    except ValueError:
        return False
    return True


# Streaming methods that return Server-Sent Events rather than buffered responses.
_STREAMING_METHODS = frozenset(
    {
        "message/stream",
        "SendStreamingMessage",
        "tasks/resubscribe",
        "TaskSubscription",
    }
)


def _is_streaming_send(body: bytes) -> bool:
    """Checks whether a JSON-RPC request calls a streaming method.

    Args:
        body: Raw request body bytes.

    Returns:
        True if the JSON-RPC payload specifies a streaming method.
    """
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("method") in _STREAMING_METHODS
    )


async def _stream_a2a(
    url: str, headers: dict[str, str], body: bytes
) -> AsyncIterator[bytes]:
    """Relays upstream SSE frames, synthesizing missing artifact updates if dropped.

    1. Streams frames incrementally from upstream without buffering.
    2. Translates non-200 responses and unframed error bodies into SSE error frames.
    3. Forwards intermediate frames, tracking task identifiers and holding the final frame.
    4. If no artifact updates were streamed before completion, fetches the task to
       reconstruct and emit the missing artifact frames.
    5. Yields the withheld final terminal frame to close the turn.

    Args:
        url: Upstream endpoint URL.
        headers: Request headers.
        body: Request body bytes.

    Yields:
        SSE data frame bytes.
    """
    saw_artifact = False
    final_frame: bytes | None = None
    task_id = context_id = rpc_id = None
    async with _create_streaming_client() as http:
        async with http.stream(
            "POST", url, headers=headers, content=body
        ) as upstream:
            if upstream.status_code != 200:
                # Without this an upstream failure is an empty stream, which
                # the browser draws as a reply that never arrives.
                detail = (await upstream.aread()).decode(errors="replace")[:500]
                yield _build_sse_frame(
                    _extract_request_id(body),
                    {"code": upstream.status_code, "message": detail},
                    key="error",
                )
                return
            if not _is_event_stream(upstream.headers.get("content-type")):
                # A rejected request is answered with 200 and a plain JSON-RPC
                # error body, no SSE framing at all: every line of it fails the
                # `data:` test below, so the relay ends having yielded nothing
                # and the browser hangs exactly as it does on a failure.
                yield _unframed_body_to_error_frame(
                    await upstream.aread(), _extract_request_id(body)
                )
                return
            async for line in upstream.aiter_lines():
                if not line.startswith("data:"):
                    continue
                frame = line[len("data:") :].strip()
                try:
                    parsed = json.loads(frame) or {}
                except ValueError:
                    yield f"data: {frame}\n\n".encode()
                    continue
                if "error" in parsed:
                    # A2A reports a refusal as a 200 carrying an error object.
                    yield f"data: {frame}\n\n".encode()
                    continue
                result = parsed.get("result") or {}
                rpc_id = parsed.get("id", rpc_id)
                task = result.get("task") if "task" in result else result
                if isinstance(task, dict) and task.get("id"):
                    task_id = task_id or task.get("id")
                    context_id = context_id or task.get("contextId")
                if (
                    "artifactUpdate" in result
                    or result.get("kind") == "artifact-update"
                ):
                    saw_artifact = True
                if _is_final_frame(result):
                    final_frame = f"data: {frame}\n\n".encode()
                    continue
                yield f"data: {frame}\n\n".encode()

    if not saw_artifact and task_id:
        for repaired in await _fetch_task_artifact_frames(
            url, headers, task_id, context_id, rpc_id
        ):
            yield repaired
    if final_frame:
        yield final_frame


# The spec dialect marks the last frame with `final: true`; the proto-JSON one
# has no such field and signals it with a terminal state instead.
_TERMINAL_STATES = frozenset(
    {
        "completed",
        "failed",
        "canceled",
        "cancelled",
        "rejected",
        "task_state_completed",
        "task_state_failed",
        "task_state_canceled",
        "task_state_cancelled",
        "task_state_rejected",
    }
)


def _is_final_frame(result: dict[str, Any]) -> bool:
    """Checks whether an SSE result payload represents a terminal turn state.

    Args:
        result: Decoded result dictionary from an SSE data frame.

    Returns:
        True if the frame signals completion, cancellation, or failure.
    """
    update = result.get("statusUpdate") if "statusUpdate" in result else result
    if not isinstance(update, dict):
        return False
    if update.get("final"):
        return True
    state = (update.get("status") or {}).get("state")
    return isinstance(state, str) and state.lower() in _TERMINAL_STATES


async def _fetch_task_artifact_frames(
    url: str,
    headers: dict[str, str],
    task_id: str,
    context_id: str | None,
    rpc_id: Any,
) -> list[bytes]:
    """Fetches a completed task to synthesize missing artifact frames.

    Args:
        url: Upstream endpoint URL.
        headers: HTTP request headers.
        task_id: Identifier of the completed task.
        context_id: Associated conversation context identifier, if any.
        rpc_id: Original JSON-RPC request identifier.

    Returns:
        List of encoded SSE artifact update frames.
    """
    request = {
        "jsonrpc": "2.0",
        "id": rpc_id if rpc_id is not None else 1,
        # `id`, not the proto-ish `name`: `GetTaskRequest` has no `name` field
        # and rejects it as invalid params.
        "method": "GetTask",
        "params": {"id": task_id},
    }
    try:
        response = await _forward(
            "POST", url, headers=headers, content=json.dumps(request).encode()
        )
        result = (response.json() or {}).get("result") or {}
    except Exception:
        # Repair is best effort: the turn's text already reached the browser,
        # and failing here would discard it along with the withheld final frame.
        return []
    task = result.get("task") if "task" in result else result
    if not isinstance(task, dict):
        return []
    return [
        _build_sse_frame(
            rpc_id,
            {
                "artifactUpdate": {
                    "taskId": task_id,
                    "contextId": context_id,
                    "artifact": artifact,
                    "append": False,
                    "lastChunk": True,
                }
            },
        )
        for artifact in task.get("artifacts") or []
    ]


def _build_sse_frame(rpc_id: Any, result: Any, key: str = "result") -> bytes:
    return (
        b"data: "
        + json.dumps({"id": rpc_id, "jsonrpc": "2.0", key: result}).encode()
        + b"\n\n"
    )


def _is_event_stream(content_type: str | None) -> bool:
    """Checks whether the Content-Type header specifies a text/event-stream.

    Args:
        content_type: Value of the Content-Type header.

    Returns:
        True if the media type is text/event-stream.
    """
    return (content_type or "").split(";")[
        0
    ].strip().lower() == "text/event-stream"


def _extract_request_id(body: bytes) -> Any:
    """Extracts the JSON-RPC request identifier from the request body.

    Args:
        body: Raw request body bytes.

    Returns:
        JSON-RPC request identifier, or None if invalid or absent.
    """
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return payload.get("id") if isinstance(payload, dict) else None


def _unframed_body_to_error_frame(body: bytes, rpc_id: Any) -> bytes:
    """Converts an unframed upstream response body into an SSE error frame.

    Args:
        body: Raw response bytes from the upstream server.
        rpc_id: JSON-RPC request identifier.

    Returns:
        Formatted SSE error frame bytes.
    """
    text = body.decode(errors="replace").strip()
    try:
        error = (json.loads(text) or {}).get("error")
    except (ValueError, AttributeError):
        error = None
    if not isinstance(error, dict):
        error = {"code": -32603, "message": text[:500] or _NOT_DEPLOYED}
    return _build_sse_frame(rpc_id, error, key="error")


def _rewrite_card_to_same_origin(content: bytes, base: str) -> bytes:
    """Rewrites absolute upstream URLs in an agent card to same-origin proxy paths.

    Args:
        content: Raw agent card JSON bytes.
        base: Upstream base URL prefix to strip.

    Returns:
        Modified agent card JSON bytes with relative proxy endpoints.
    """
    try:
        card = json.loads(content)
    except ValueError:
        return content

    def rewrite(url: object) -> object:
        if isinstance(url, str) and url.startswith(base):
            return url[len(base) :] or "/"
        return url

    if "url" in card:
        card["url"] = rewrite(card["url"])
    for key in ("supportedInterfaces", "additionalInterfaces"):
        for interface in card.get(key) or []:
            if isinstance(interface, dict) and "url" in interface:
                interface["url"] = rewrite(interface["url"])
    return json.dumps(card).encode()


# --------------------------------------------------------------------------- #
# `/lha/*` -- surfaces the replacement dashboard calls that AQuA does not have.
#
# These answer 501, and the wording matters. An empty object renders as
# "nothing here", which is indistinguishable from a real empty state and is a
# lie about a surface that does not exist; the client also short-circuits on
# 501 and would otherwise retry a route that will never appear. Two exceptions:
# `/lha/sessions`, whose list genuinely is client-side, and `/lha/contexts`,
# which is real and backed by the task store.
# --------------------------------------------------------------------------- #

# `/lha` originates from the Long Horizon Agent implementation:
# https://github.com/google/adk-samples/tree/main/core/python/long-horizon-harness

_LHA_ABSENT = {
    "state": "AQuA has no workspace state; the dashboard reads /api/config.",
    "workspace": "AQuA watches one deployed agent, so there is no workspace tree.",
    "uploads": "AQuA ingests telemetry from the observed agent, not uploads.",
    "feedback": "Feedback is filed as an issue in google/adk-recipes.",
}


def _build_lha_unavailable_response(area: str) -> JSONResponse:
    """Returns an HTTP 501 Not Implemented response for unsupported LHA routes.

    Args:
        area: Route area name (e.g. workspace, uploads, feedback).

    Returns:
        JSONResponse with HTTP 501 status and error details.
    """
    return JSONResponse(
        status_code=501,
        content={
            "error": "not available in AQuA",
            "area": area,
            "detail": _LHA_ABSENT.get(area, "Not implemented in AQuA."),
        },
    )


# --------------------------------------------------------------------------- #
# The goal and the memories
#
# Pass-throughs, like every other panel: the agent owns these documents, so the
# object layout, the goal's versions, the memory ids and the write paths live
# there and this process only relays.
# Reading them from GCS here as well would be a second implementation of the
# same contract, and the two would drift.
# --------------------------------------------------------------------------- #


@app.get("/api/goal")
async def get_goal() -> dict:
    """Retrieves the active quality goal and version for the observed agent.

    Returns:
        Dictionary containing the goal text and active version or error details.
    """
    try:
        result = await _get_backend_client().post_command(GOAL_GET_ROUTE, {})
    except Exception as exc:
        return {
            "goal": None,
            "available": False,
            "reason": _format_error(exc),
        }
    if not result:
        return {
            "goal": None,
            "available": False,
            "reason": "The agent returned no goal payload.",
        }
    return result


@app.put("/api/goal")
async def put_goal(request: Request) -> Any:
    """Updates the quality goal for the observed agent.

    Args:
        request: Incoming HTTP request with JSON body containing `goal`.

    Returns:
        JSON response with the updated goal or HTTP error.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)
    try:
        result = await _get_backend_client().post_command(
            GOAL_SET_ROUTE, {"goal": str((body or {}).get("goal") or "")}
        )
    except Exception as exc:
        return JSONResponse({"error": _format_error(exc)}, status_code=502)
    if result.get("error"):
        # The agent refuses a goal over 8 KiB; that is the caller's mistake,
        # not a failure of this service.
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


@app.get("/api/goal/versions")
async def get_goal_versions() -> dict:
    """Lists every saved version of the goal, most recently active first.

    Restoring one is a PUT of its text to /api/goal.

    Returns:
        Dictionary containing the versions or error details.
    """
    try:
        result = await _get_backend_client().post_command(
            GOAL_VERSIONS_ROUTE, {}
        )
    except Exception as exc:
        return {
            "versions": [],
            "available": False,
            "reason": _format_error(exc),
        }
    if not result:
        return {
            "versions": [],
            "available": False,
            "reason": "The agent returned no goal versions payload.",
        }
    return result


@app.get("/api/memories")
async def list_memories() -> dict:
    """Lists the memories the developer asked the chat to keep, newest first.

    Returns:
        Dictionary containing the memories or error details.
    """
    try:
        result = await _get_backend_client().post_command(
            MEMORIES_LIST_ROUTE, {}
        )
    except Exception as exc:
        return {
            "memories": [],
            "available": False,
            "reason": _format_error(exc),
        }
    if not result:
        return {
            "memories": [],
            "available": False,
            "reason": "The agent returned no memories payload.",
        }
    return result


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: str) -> Any:
    """Deletes a memory by identifier.

    Args:
        memory_id: Identifier of the memory to delete.

    Returns:
        JSON response confirming deletion or HTTP error.
    """
    try:
        result = await _get_backend_client().post_command(
            MEMORIES_DELETE_ROUTE, {"memory_id": memory_id}
        )
    except Exception as exc:
        return JSONResponse({"error": _format_error(exc)}, status_code=502)
    if result.get("error"):
        return JSONResponse({"error": result["error"]}, status_code=404)
    return result


def _read_jobs_bucket() -> str:
    """Reads the configured Cloud Storage jobs bucket name from the environment.

    Returns:
        Bucket name string, or empty string if unset.
    """
    return os.getenv("AQA_JOBS_GCS_BUCKET", "")


async def _list_contexts() -> Any:
    """Lists conversations stored in Cloud Storage, ordered newest first.

    Returns:
        Dictionary mapping "contexts" to conversation summaries, or error response.
    """
    bucket = _read_jobs_bucket()
    if not bucket:
        return _build_lha_unavailable_response("contexts")
    try:
        contexts = await task_reader.list_contexts(bucket)
    except Exception as exc:
        return JSONResponse(
            {"contexts": [], "error": f"{type(exc).__name__}: {exc}"},
            status_code=502,
        )
    return {"contexts": contexts}


@app.get("/lha/contexts")
@app.get("/lha/contexts/")
async def list_lha_contexts() -> Any:
    return await _list_contexts()


@app.get("/lha/contexts/{context_id}/tasks")
async def list_lha_context_tasks(context_id: str) -> Any:
    """Retrieves task transcripts for a conversation context, ordered chronologically.

    Args:
        context_id: Conversation context identifier.

    Returns:
        Dictionary mapping "tasks" to the list of task records, or error response.
    """
    bucket = _read_jobs_bucket()
    if not bucket:
        return _build_lha_unavailable_response("contexts")
    try:
        tasks = await task_reader.list_context_tasks(bucket, context_id)
    except Exception as exc:
        return JSONResponse(
            {"tasks": [], "error": f"{type(exc).__name__}: {exc}"},
            status_code=502,
        )
    return {"tasks": tasks}


@app.delete("/lha/contexts/{context_id}")
async def delete_lha_context(context_id: str) -> Any:
    """Deletes all task blobs associated with a conversation context from storage.

    Removing the underlying task blobs ensures the deleted conversation is not
    resurfaced during subsequent context listings.

    Args:
        context_id: Identifier of the conversation context to delete.

    Returns:
        Dict mapping "deleted" to the number of erased task blobs, or an HTTP
        501/502 JSONResponse on failure.
    """
    bucket = _read_jobs_bucket()
    if not bucket:
        return _build_lha_unavailable_response("contexts")
    try:
        deleted = await task_reader.delete_context(bucket, context_id)
    except Exception as exc:
        # Omit count on failure because partial deletes leave the remaining count indeterminate.
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"}, status_code=502
        )
    return {"deleted": deleted}


@app.api_route(
    "/lha/sessions", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
@app.api_route(
    "/lha/sessions/", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
def list_lha_sessions() -> Any:
    """Returns an empty session list for client-managed sessions.

    Returns:
        Dictionary with an empty sessions list and note.
    """
    return {"sessions": [], "note": "Conversations are held client-side."}


# Declared after the named routes above, so they never shadow them. Both
# shapes exist because `/lha/uploads` and `/lha/uploads/x` are separate paths
# to FastAPI, and wiring one leaves the other unanswered.
@app.api_route("/lha/{area}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def report_lha_area_unavailable(area: str) -> Any:
    return _build_lha_unavailable_response(area)


@app.api_route(
    "/lha/{area}/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
def report_lha_subpath_unavailable(area: str, rest: str) -> Any:
    del rest
    return _build_lha_unavailable_response(area)


@app.api_route("/feedback", methods=["GET", "POST"])
def report_feedback_unavailable() -> Any:
    return _build_lha_unavailable_response("feedback")


# --------------------------------------------------------------------------- #
# The front end. Everything below is registered last, on purpose: FastAPI
# matches routes in registration order, and the catch-all at the end matches
# every path there is. Declared any earlier it would shadow /api/*.
# --------------------------------------------------------------------------- #

# First path segments the catch-all must not answer with the HTML shell. Ours,
# so a miss is a genuine 404:
_OURS_404 = frozenset({"api", "healthz"})
# Data prefixes the front end calls. All four are served for real further up --
# `/a2a` and the agent card are proxied, `/lha/*` and `/feedback` answer above --
# and are listed here anyway: this set is a backstop, and its value is that it
# holds whether or not a route exists. Answering a data request with the HTML
# shell is the failure being prevented -- 200 plus a `<!doctype html>` reads to
# `fetch` as success -- and the way that happens is somebody adding or moving a
# route without noticing the catch-all below. It still answers the gaps the
# routes above leave: bare `/lha`, `/feedback/<anything>`, any other
# `/.well-known/*`.
_NOT_IMPLEMENTED_501 = frozenset({"lha", "a2a", ".well-known", "feedback"})

if BUNDLE_AVAILABLE:
    # Hashed filenames, so they are immutable and worth a real static handler.
    # Vite emits them as absolute /assets/... because `base` is "/".
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(STATIC_V2_DIR, "assets")),
        name="assets_v2",
    )

    @app.get("/v2")
    @app.get("/v2/{sub_path:path}")
    async def redirect_v2_path(
        request: Request, sub_path: str = ""
    ) -> RedirectResponse:
        """Permanently redirects /v2 paths to root-relative paths.

        Uses HTTP 308 so request method and body are preserved across the redirect.

        Args:
            request: Incoming HTTP request.
            sub_path: Trailing path segments to preserve.

        Returns:
            RedirectResponse with HTTP 308 status code.
        """
        query = request.url.query
        return RedirectResponse(
            f"/{sub_path}" + (f"?{query}" if query else ""), status_code=308
        )

    @app.get("/{spa_path:path}")
    async def serve_spa(spa_path: str) -> FileResponse:
        """Serves static assets or falls back to the SPA HTML shell.

        Routes belonging to the API or unsupported integrations return 404 or 501
        to prevent HTML fallback responses on data requests.

        Args:
            spa_path: Requested path within the single-page application.

        Returns:
            FileResponse serving the static file or index.html.

        Raises:
            HTTPException: 404 for missing API routes; 501 for unimplemented endpoints.
        """
        head = spa_path.split("/", 1)[0]
        if head in _OURS_404:
            raise HTTPException(status_code=404, detail="Not Found")
        if head in _NOT_IMPLEMENTED_501:
            # The contract the front end is written against: it short-circuits
            # on a 501 and would retry against anything else. A2A's routes and
            # the real /lha/contexts land as explicit routes above this one.
            raise HTTPException(
                status_code=501,
                detail=f"/{head} is not available in AQuA yet.",
            )
        candidate = os.path.normpath(os.path.join(STATIC_V2_DIR, spa_path))
        if (
            spa_path
            and os.path.commonpath([candidate, STATIC_V2_DIR]) == STATIC_V2_DIR
            and os.path.isfile(candidate)
        ):
            return FileResponse(candidate)
        return FileResponse(V2_INDEX)


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104 - direct runs mirror the container CMD's all-interfaces bind


if __name__ == "__main__":
    main()
