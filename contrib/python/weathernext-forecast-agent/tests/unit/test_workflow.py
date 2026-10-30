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
"""Unit tests for the workflow's function nodes and routing. No model calls.

Function nodes are called directly. End-to-end routing runs the real
workflow with `Gemini.generate_content_async` replaced by a fake that returns
canned structured requests for `parse_request` and a marker text for the
explain agents.
"""

import asyncio
import json
from collections.abc import AsyncGenerator

import pytest
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from app import agent, forecast

ALL_SITES = ["whitelee", "clyde", "black-law"]


class FakeContext:
    """The only part of `Context` the function nodes use."""

    def __init__(self):
        self.state = {}


@pytest.fixture(autouse=True)
def sample_source(monkeypatch):
    monkeypatch.setenv("WEATHERNEXT_SOURCE", "sample")
    monkeypatch.delenv("TASK_LIMITS_JSON", raising=False)
    forecast.get_source.cache_clear()
    yield
    forecast.get_source.cache_clear()


def plan_request(**overrides):
    request = {
        "intent": "plan",
        "task_type": "main_component_lift",
        "site_ids": [],
        "crews": 2,
        "horizon_hours": 120,
        "min_hours": 0,
    }
    request.update(overrides)
    return request


def run_plan(request):
    """Runs check -> load -> find -> plan on a request; returns (route, output)."""
    ctx = FakeContext()
    checked = agent.check_request(request, ctx)
    assert checked.actions.route == agent.ROUTE_PLAN
    loaded = agent.load_forecasts(checked.output, ctx)
    assert loaded.actions.route == agent.ROUTE_OK
    found = agent.find_windows(loaded.output)
    planned = agent.plan_crews(found.output)
    return planned.actions.route, planned.output


# --- check_request --------------------------------------------------------


def test_check_request_plan_fills_all_sites():
    event = agent.check_request(plan_request(), FakeContext())
    assert event.actions.route == agent.ROUTE_PLAN
    assert event.output["site_ids"] == ALL_SITES


def test_check_request_help_routes_to_reply():
    event = agent.check_request({"intent": "help"}, FakeContext())
    assert event.actions.route == agent.ROUTE_REPLY
    assert event.output["problems"] == []


def test_check_request_retries_then_replies(monkeypatch):
    monkeypatch.setenv("PARSE_RETRY_LIMIT", "1")
    ctx = FakeContext()
    bad = plan_request(task_type="painting", site_ids=["atlantis"])
    first = agent.check_request(bad, ctx)
    assert first.actions.route == agent.ROUTE_RETRY
    errors = " ".join(first.output["validation_errors"])
    assert "painting" in errors and "atlantis" in errors
    second = agent.check_request(bad, ctx)
    assert second.actions.route == agent.ROUTE_REPLY
    assert len(second.output["problems"]) == 2
    # The budget resets once the request is settled.
    assert ctx.state["parse_attempts"] == 0


def test_check_request_rejects_bad_numbers(monkeypatch):
    monkeypatch.setenv("PARSE_RETRY_LIMIT", "0")
    event = agent.check_request(
        plan_request(crews=0, horizon_hours=0, min_hours=-1), FakeContext()
    )
    assert event.actions.route == agent.ROUTE_REPLY
    assert len(event.output["problems"]) == 3


# --- deterministic replies and data errors --------------------------------


def test_reply_without_plan_lists_sites_tasks_and_problems():
    event = agent.reply_without_plan({"problems": ["unknown site_ids ['x']"]})
    text = event.output["reply"]
    assert "unknown site_ids" in text
    for sid in ALL_SITES:
        assert sid in text
    assert "main_component_lift" in text
    assert event.content.role == "model"


def test_load_forecasts_routes_error(monkeypatch):
    class Broken:
        description = "broken"

        def get(self, site):
            raise PermissionError("no WeatherNext access")

    monkeypatch.setattr(agent, "get_source", Broken)
    event = agent.load_forecasts(
        plan_request(site_ids=ALL_SITES), FakeContext()
    )
    assert event.actions.route == agent.ROUTE_ERROR
    assert "PermissionError" in event.output["error"]


# --- find_windows / plan_crews / suggest_alternatives ---------------------


def test_plan_go_for_main_component_lift():
    # Bundled snapshot: ECMWF ENS run 2026-10-19 06 UTC, 51 members.
    route, out = run_plan(plan_request())
    assert route == agent.ROUTE_GO
    got = {a["site_id"]: a for a in out["assignments"]}
    assert set(got) == set(ALL_SITES)
    assert out["unassigned_sites"] == {}
    # Thursday 22 Oct is the best lift day at every site, but two crews cannot
    # cover three farms that day: one site falls back to Wednesday.
    thursday = [
        a for a in got.values() if a["start_local"].startswith("2026-10-22")
    ]
    assert len(thursday) == 2
    assert all(
        a["probability_safe"] == 0.98 and a["members_safe"] == 50
        for a in thursday
    )
    assert got["whitelee"]["start_local"] == "2026-10-21T08:00:00+01:00"
    assert got["whitelee"]["members_safe"] == 43
    assert out["expected_completed_jobs"] == 2.8
    for windows in out["best_windows_by_site"].values():
        assert len(windows) <= agent.SHOWN_WINDOWS_PER_SITE
        assert all("start_local" in w for w in windows)
    json.dumps(out)  # event outputs must be JSON-serializable


def test_plan_no_go_and_alternatives():
    # A lift at Whitelee tomorrow only (Tue 20 Oct): best window is 64.7%.
    route, out = run_plan(
        plan_request(site_ids=["whitelee"], crews=1, horizon_hours=24)
    )
    assert route == agent.ROUTE_NO_GO
    assert out["assignments"] == []
    best = out["best_windows_by_site"]["whitelee"][0]
    assert best["probability_safe"] == 0.647
    assert (
        best["fail_fraction_precipitation"] > best["fail_fraction_wind_or_gust"]
    )
    alt = agent.suggest_alternatives(out).output
    tasks = {a["task_type"]: a for a in alt["alternative_tasks"]}
    assert set(tasks) == {"blade_repair", "nacelle_service"}
    assert tasks["nacelle_service"]["probability_safe"] == 1.0
    outlook = alt["daily_outlook_by_site"]["whitelee"]
    assert len(outlook) == 1
    assert all(b["hours"] <= agent.OUTLOOK_BLOCK_HOURS for b in outlook)
    json.dumps(alt)


def test_window_round_trip_through_dict():
    _, out = run_plan(plan_request(task_type="blade_repair", crews=1))
    best = out["best_windows_by_site"]["clyde"][0]
    window = agent.analysis.Window.from_dict(best)
    sites = forecast.load_sites()
    again = window.to_dict(sites["clyde"])
    assert again == best


# --- end-to-end routing with a fake model ---------------------------------


def _fake_llm(parse_outputs):
    calls = []

    async def generate(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        instruction = str(llm_request.config.system_instruction or "")
        if "structured planning" in instruction:
            text = json.dumps(parse_outputs.pop(0))
            calls.append("parse_request")
        elif "NO window meets" in instruction:
            text = "EXPLAIN_NO_WINDOW"
            calls.append("explain_no_window")
        else:
            text = "EXPLAIN_PLAN"
            calls.append("explain_plan")
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)])
        )

    return generate, calls


def _run_workflow(monkeypatch, parse_outputs, prompt="plan please"):
    generate, calls = _fake_llm(list(parse_outputs))
    monkeypatch.setattr(Gemini, "generate_content_async", generate)

    async def go():
        runner = InMemoryRunner(app=agent.app)
        session = await runner.session_service.create_session(
            app_name="app", user_id="u"
        )
        msg = types.Content(role="user", parts=[types.Part(text=prompt)])
        nodes, final = [], None
        async for event in runner.run_async(
            user_id="u", session_id=session.id, new_message=msg
        ):
            path = getattr(event.node_info, "path", "") or ""
            if path:
                nodes.append(path.rsplit("/", 1)[-1].split("@")[0])
            if event.content and event.content.parts:
                text = "".join(p.text or "" for p in event.content.parts)
                if text:
                    final = text
        return nodes, final

    nodes, final = asyncio.run(go())
    return nodes, final, calls


def test_workflow_go_route(monkeypatch):
    nodes, final, calls = _run_workflow(monkeypatch, [plan_request()])
    assert calls == ["parse_request", "explain_plan"]
    for name in (
        "check_request",
        "load_forecasts",
        "find_windows",
        "plan_crews",
    ):
        assert name in nodes
    assert "suggest_alternatives" not in nodes
    assert final == "EXPLAIN_PLAN"


def test_workflow_no_go_route(monkeypatch):
    request = plan_request(site_ids=["whitelee"], crews=1, horizon_hours=24)
    nodes, final, calls = _run_workflow(monkeypatch, [request])
    assert calls == ["parse_request", "explain_no_window"]
    assert "suggest_alternatives" in nodes
    assert final == "EXPLAIN_NO_WINDOW"


def test_workflow_help_route_needs_no_explain_model(monkeypatch):
    nodes, final, calls = _run_workflow(monkeypatch, [{"intent": "help"}])
    assert calls == ["parse_request"]
    assert "reply_without_plan" in nodes
    assert "whitelee" in final


def test_workflow_retry_loop_is_bounded(monkeypatch):
    monkeypatch.setenv("PARSE_RETRY_LIMIT", "1")
    bad = plan_request(task_type="crane_lift")
    good = plan_request(task_type="main_component_lift")
    nodes, final, calls = _run_workflow(monkeypatch, [bad, good])
    assert calls == ["parse_request", "parse_request", "explain_plan"]
    assert "load_forecasts" in nodes
    assert final == "EXPLAIN_PLAN"

    _, final, calls = _run_workflow(monkeypatch, [bad, bad])
    assert calls == ["parse_request", "parse_request"]
    assert "crane_lift" in final
