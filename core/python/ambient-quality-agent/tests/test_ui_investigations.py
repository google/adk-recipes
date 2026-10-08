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

"""Hermetic tests for the AQA UI dashboard endpoints.

Drives the FastAPI app in-process with ``httpx.ASGITransport`` (no network) and
a stub client injected via ``app._get_backend_client``. The stub answers ``post_command()``
with the payload the matching agent command route returns, so these tests pin
the exact seam the dashboard relies on -- for all three of its panels
(configuration, investigations, insights).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

# The UI is not a package -- it is flattened to the image root -- so `app` is
# a top-level module. `pythonpath` in pyproject.toml puts both roots in place.
import app as ui_app

# --------------------------------------------------------------------------- #
# Stub client                                                                  #
# --------------------------------------------------------------------------- #


class FakeClient:
    """Stub agent client whose ``post_command()`` answers with canned route payloads.

    The dashboard POSTs one command route per action (see
    `ambient_quality_agent.core.command_routes`) through the shared client's
    ``post_command(path, payload)``. This stub dispatches on that path and returns
    what the route's tool returns.
    """

    def __init__(
        self,
        runs: list[dict[str, Any]] | None = None,
        schedule_run: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        insights: dict[str, Any] | None = None,
        insight_detail: dict[str, Any] | None = None,
        insight_actions: dict[str, Any] | None = None,
        stats: dict[str, Any] | None = None,
        daily: dict[str, Any] | None = None,
        documents: dict[str, Any] | None = None,
        health: dict[str, Any] | None = None,
        unpublished: dict[str, Any] | None = None,
        case: dict[str, Any] | None = None,
    ) -> None:
        self.runs = runs or []
        self.schedule_run = schedule_run
        self.config = config
        self.stats = stats
        self.daily = daily
        self.health = health
        self.case = case
        self.unpublished: dict[str, Any] = unpublished or {}
        """Canned payloads for `source`, keyed by route path. It answers
        `available: false` in every deployment today."""
        self.insights = insights
        self.insight_detail = insight_detail
        self.insight_actions: dict[str, Any] = insight_actions or {}
        """Canned payloads for `insights/dismiss` and `insights/merge`, keyed by
        route path; the success answer when a test names neither."""
        self.documents: dict[str, Any] = documents or {}
        """Canned payloads for the `documents/*` routes, keyed by route path."""
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.runs_result: dict[str, Any] | None = None
        """Whole `list_investigations` payload, when a test needs one carrying an
        ``error`` alongside its (empty) ``runs``; otherwise built from `runs`."""

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, payload))

        if path == "investigations/daily":
            return self.daily or {"days": []}
        if path == "investigations/list":
            if self.runs_result is not None:
                return self.runs_result
            return {"runs": self.runs}
        if path == "investigations/schedule":
            return self.schedule_run if self.schedule_run is not None else {}
        if path == "investigations/get":
            run_id = (payload or {}).get("run_id")
            run = next(
                (r for r in self.runs if r.get("run_id") == run_id), None
            )
            return run or {"error": f"No run found with id {run_id!r}."}
        if path == "insights/list":
            return self.insights if self.insights is not None else {}
        if path == "insights/get":
            return (
                self.insight_detail if self.insight_detail is not None else {}
            )
        if path == "insights/dismiss":
            return self.insight_actions.get(path, {"dismissed": True})
        if path == "insights/merge":
            return self.insight_actions.get(path, {"merged": True})
        if path == "investigations/stats":
            return self.stats if self.stats is not None else {}
        if path == "config":
            return {"config": self.config} if self.config is not None else {}
        if path.startswith("documents/"):
            return self.documents.get(path, {})
        if path == "health":
            return self.health if self.health is not None else {}
        if path == "source":
            return self.unpublished.get(path, {})
        if path == "trajectories/case":
            return self.case if self.case is not None else {}
        raise AssertionError(f"unexpected command path {path!r}")

    def args_for(self, path: str) -> dict[str, Any]:
        """Returns the arguments the dashboard sent to `path` or an empty dictionary.

        Args:
            path: Target command path to look up.

        Returns:
            Dictionary payload sent to the command path.
        """
        return (
            next(payload for called, payload in self.calls if called == path)
            or {}
        )


class BoomClient:
    """Stub whose ``post_command()`` raises, to exercise the error path."""

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("backend exploded")


# --------------------------------------------------------------------------- #
# Request helpers                                                              #
# --------------------------------------------------------------------------- #


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def _get(path: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_request("GET", path, **kwargs))


def _post(path: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_request("POST", path, **kwargs))


def _put(path: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_request("PUT", path, **kwargs))


def _delete(path: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_request("DELETE", path, **kwargs))


def _inject(monkeypatch: Any, client: Any) -> None:
    """Point the app's per-conversation client factory at ``client``.

    ``monkeypatch`` restores the real ``_get_backend_client`` after the test.
    """
    monkeypatch.setattr(ui_app, "_get_backend_client", lambda: client)


# --------------------------------------------------------------------------- #
# GET /api/investigations                                                      #
# --------------------------------------------------------------------------- #

_RUNS: list[dict[str, Any]] = [
    {
        "run_id": "aaa11111",
        "status": "done",
        "observed_agent_name": "poem_agent",
        "created_at": "2026-07-03T10:00:00+00:00",
        "events": [
            {
                "created_at": "2026-07-03T10:00:01+00:00",
                "text": "hi",
                "source": "init",
            }
        ],
    },
    {
        "run_id": "bbb22222",
        "status": "pending",
        "observed_agent_name": "it_support_agent",
        "created_at": "2026-07-04T11:00:00+00:00",
    },
]


def test_list_investigations_returns_runs(monkeypatch) -> None:
    fake = FakeClient(runs=_RUNS)
    _inject(monkeypatch, fake)
    r = _get("/api/investigations?conversationId=c1")
    assert r.status_code == 200
    body = r.json()
    assert [run["run_id"] for run in body["runs"]] == ["aaa11111", "bbb22222"]
    # The dashboard drove the list command over its own route.
    assert fake.calls[0] == ("investigations/list", None)


def test_list_investigations_forwards_a_period(monkeypatch) -> None:
    """The list is scoped by the same window the totals are, so one period
    control cannot leave the table showing runs its figures exclude."""
    fake = FakeClient(runs=_RUNS)
    _inject(monkeypatch, fake)
    r = _get(
        "/api/investigations?windowStart=2026-08-01T00:00:00%2B00:00&windowEnd=2026-08-08"
    )
    assert r.status_code == 200
    assert fake.args_for("investigations/list") == {
        "window_start": "2026-08-01T00:00:00+00:00",
        "window_end": "2026-08-08",
    }


def test_list_investigations_without_a_period_sends_no_window(
    monkeypatch,
) -> None:
    """No period means no window arguments at all, so the tool applies its own
    defaults rather than being handed two empty strings to interpret."""
    fake = FakeClient(runs=_RUNS)
    _inject(monkeypatch, fake)
    _get("/api/investigations")
    assert fake.calls[0] == ("investigations/list", None)


def test_list_insights_forwards_a_period(monkeypatch) -> None:
    """Scoped on last sighting, alongside any status filter rather than
    replacing it."""
    fake = FakeClient(insights=_INSIGHTS)
    _inject(monkeypatch, fake)
    r = _get("/api/insights?status=NEW&windowStart=2026-08-01T00:00:00%2B00:00")
    assert r.status_code == 200
    assert fake.args_for("insights/list") == {
        "status": "NEW",
        "window_start": "2026-08-01T00:00:00+00:00",
    }


def test_list_investigations_empty(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(runs=[]))
    r = _get("/api/investigations")
    assert r.status_code == 200
    assert r.json() == {"runs": []}


def test_list_investigations_error_degrades(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())
    r = _get("/api/investigations")
    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == []
    assert "backend exploded" in body["error"]


def test_list_investigations_forwards_tool_error(monkeypatch) -> None:
    # The tool reports a failed BigQuery read in the payload rather than by
    # raising: `{"runs": [], "error": ...}`. Dropping the key would draw the
    # outage as "No investigations yet", so it has to reach the SPA.
    fake = FakeClient()
    fake.runs_result = {
        "runs": [],
        "error": "failed to list investigations: boom",
    }
    _inject(monkeypatch, fake)
    r = _get("/api/investigations")
    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == []
    assert "failed to list investigations" in body["error"]


# --------------------------------------------------------------------------- #
# POST /api/investigations                                                     #
# --------------------------------------------------------------------------- #


def test_start_investigation_ok_without_run(monkeypatch) -> None:
    # Scheduling yields no structured run record here -> {"ok": true}.
    _inject(monkeypatch, FakeClient(schedule_run=None))
    r = _post("/api/investigations")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_start_investigation_returns_run(monkeypatch) -> None:
    run = {"run_id": "ccc33333", "status": "pending"}
    _inject(monkeypatch, FakeClient(schedule_run=run))
    r = _post("/api/investigations")
    assert r.status_code == 200
    assert r.json() == {"run": run}


def test_start_investigation_error_degrades(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())
    r = _post("/api/investigations")
    assert r.status_code == 200
    assert "backend exploded" in r.json()["error"]


def test_start_investigation_error_wins_over_run_id(monkeypatch) -> None:
    # A run whose record was written but whose job failed to submit comes back
    # with BOTH an error and a run_id (`format_run` always has one). The error
    # has to win, or every such failure is reported to the user as "started".
    failed = {
        "run_id": "ddd44444",
        "status": "failed",
        "error": "failed to submit the durable job: quota exceeded",
    }
    _inject(monkeypatch, FakeClient(schedule_run=failed))
    r = _post("/api/investigations")
    assert r.status_code == 200
    body = r.json()
    assert "quota exceeded" in body["error"]
    assert "run" not in body


# --------------------------------------------------------------------------- #
# GET /api/investigations/{run_id}                                             #
# --------------------------------------------------------------------------- #


def test_get_investigation_record(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(runs=_RUNS))
    r = _get("/api/investigations/aaa11111")
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["run_id"] == "aaa11111"
    # The detail panel's payload carries the run's events; the list's does not.
    assert run["events"][0]["source"] == "init"


def test_get_investigation_not_found(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(runs=_RUNS))
    r = _get("/api/investigations/missing")
    assert r.status_code == 200
    assert "error" in r.json()
    assert "run" not in r.json()


# --------------------------------------------------------------------------- #
# GET /api/stats                                                               #
# --------------------------------------------------------------------------- #

_STATS = {"investigations": 3, "traces_scanned": 900, "traces_eval_failed": 12}


def test_stats_returns_the_totals(monkeypatch) -> None:
    fake = FakeClient(stats={"stats": _STATS})
    _inject(monkeypatch, fake)
    r = _get("/api/stats?conversationId=c1")
    assert r.status_code == 200
    assert r.json()["stats"]["traces_scanned"] == 900
    # Unscoped totals send no window arguments.
    assert fake.calls[0] == ("investigations/stats", None)


def test_stats_forwards_a_period(monkeypatch) -> None:
    """The window reaches the agent as command arguments, so the totals are
    scoped in BigQuery rather than added up from the capped runs list."""
    fake = FakeClient(
        stats={"stats": _STATS, "window": {"start": "s", "end": "e"}}
    )
    _inject(monkeypatch, fake)
    r = _get(
        "/api/stats?windowStart=2026-08-01T00:00:00%2B00:00&windowEnd=2026-08-08"
    )
    assert r.status_code == 200
    assert fake.args_for("investigations/stats") == {
        "window_start": "2026-08-01T00:00:00+00:00",
        "window_end": "2026-08-08",
    }
    # Echoed back, so the caller labels the figures with the period applied.
    assert r.json()["window"] == {"start": "s", "end": "e"}


def test_daily_returns_the_buckets(monkeypatch) -> None:
    """Aggregated by the agent over the period, not bucketed out of the runs
    list -- which returns 50 sweeps and would chart a fraction of a long
    period as though it were all of it."""
    days = [{"day": "2026-08-20", "investigations": 4, "traces_eval_failed": 9}]
    fake = FakeClient(daily={"days": days})
    _inject(monkeypatch, fake)
    r = _get("/api/daily?windowStart=2026-08-01T00:00:00%2B00:00")
    assert r.status_code == 200
    assert r.json()["days"] == days
    assert fake.args_for("investigations/daily") == {
        "window_start": "2026-08-01T00:00:00+00:00"
    }


def test_daily_forwards_a_read_failure(monkeypatch) -> None:
    """An empty day list reads as a quiet period, so a failed read has to say
    it failed rather than degrade into one."""
    fake = FakeClient(daily={"days": [], "error": "bigquery exploded"})
    _inject(monkeypatch, fake)
    r = _get("/api/daily")
    assert r.status_code == 200
    assert r.json()["error"] == "bigquery exploded"


def test_stats_forwards_a_read_failure(monkeypatch) -> None:
    """All-zero totals read as a quiet deployment, so a failed read has to say
    it failed rather than degrade into them."""
    _inject(
        monkeypatch, FakeClient(stats={"stats": {}, "error": "no such table"})
    )
    r = _get("/api/stats")
    assert r.status_code == 200
    assert r.json()["error"] == "no such table"


def test_stats_error_degrades(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())
    r = _get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] == {} and "error" in body


# --------------------------------------------------------------------------- #
# GET /api/config                                                              #
# --------------------------------------------------------------------------- #

_CONFIG = {
    "observed_agent_name": "poem_agent",
    "data_lookback_window": 7,
    "multi_turn_metrics": ["task_success"],
}


def test_config_returns_effective_config(monkeypatch) -> None:
    fake = FakeClient(config=_CONFIG)
    _inject(monkeypatch, fake)
    r = _get("/api/config")
    assert r.status_code == 200
    assert r.json()["config"]["data_lookback_window"] == 7
    # The config read takes no arguments.
    assert fake.calls[0] == ("config", None)


def test_config_without_payload_errors(monkeypatch) -> None:
    # The turn produced no `show_config` result -> a clean error, not a 5xx.
    _inject(monkeypatch, FakeClient(config=None))
    r = _get("/api/config")
    assert r.status_code == 200
    assert "error" in r.json()


def test_config_error_degrades(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())
    r = _get("/api/config")
    assert r.status_code == 200
    assert "backend exploded" in r.json()["error"]


# --------------------------------------------------------------------------- #
# GET /api/insights                                                            #
# --------------------------------------------------------------------------- #

_INSIGHTS = {
    "insights": [
        {
            "insight_id": "ins-1",
            "label": "stopped before completing the request",
            "status": "NEW",
            "occurrence_count": 2,
            "last_run_id": "aaa11111",
        }
    ],
    "total": 1,
    "next_page_token": None,
}


def test_list_insights_returns_page(monkeypatch) -> None:
    fake = FakeClient(insights=_INSIGHTS)
    _inject(monkeypatch, fake)
    r = _get("/api/insights")
    assert r.status_code == 200
    body = r.json()
    assert [i["insight_id"] for i in body["insights"]] == ["ins-1"]
    assert body["total"] == 1
    # No filters -> none sent, so the tool applies its own defaults.
    assert fake.args_for("insights/list") == {}


def test_list_insights_forwards_status_filter(monkeypatch) -> None:
    fake = FakeClient(insights=_INSIGHTS)
    _inject(monkeypatch, fake)
    r = _get("/api/insights?status=NEW")
    assert r.status_code == 200
    assert fake.args_for("insights/list")["status"] == "NEW"


def test_list_insights_forwards_the_day(monkeypatch) -> None:
    fake = FakeClient(insights=_INSIGHTS)
    _inject(monkeypatch, fake)

    r = _get("/api/insights?day=2026-09-20&status=RESOLVED")

    assert r.status_code == 200
    args = fake.args_for("insights/list")
    assert args["day"] == "2026-09-20"
    assert args["status"] == "RESOLVED"


def test_list_insights_forwards_the_page_the_client_wants(monkeypatch) -> None:
    """Size, order and token reach the engine, which is where all three apply.

    The dashboard asks for the rows it draws in the order it draws them, so
    that a page is one read and the ranking covers every match rather than
    whatever the page happened to hold.
    """
    fake = FakeClient(insights=_INSIGHTS)
    _inject(monkeypatch, fake)

    r = _get("/api/insights?pageSize=10&orderBy=impact&pageToken=tok-2")

    assert r.status_code == 200
    args = fake.args_for("insights/list")
    assert args["page_size"] == 10
    assert args["order_by"] == "impact"
    assert args["page_token"] == "tok-2"


def test_list_insights_passes_the_next_page_token_back(monkeypatch) -> None:
    fake = FakeClient(
        insights={**_INSIGHTS, "total": 87, "next_page_token": "tok-2"},
    )
    _inject(monkeypatch, fake)

    body = _get("/api/insights").json()

    assert body["next_page_token"] == "tok-2"
    assert body["total"] == 87


def test_list_insights_ignores_an_unreadable_page_size(monkeypatch) -> None:
    # Dropped rather than rejected: the engine's own default answers the
    # request, and a 400 would blank a list over a malformed query param.
    fake = FakeClient(insights=_INSIGHTS)
    _inject(monkeypatch, fake)

    r = _get("/api/insights?pageSize=ten")

    assert r.status_code == 200
    assert "page_size" not in fake.args_for("insights/list")


def test_list_insights_error_degrades(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())
    r = _get("/api/insights")
    assert r.status_code == 200
    body = r.json()
    assert body["insights"] == []
    assert "backend exploded" in body["error"]


def test_list_insights_passes_the_conversation_count_through(
    monkeypatch,
) -> None:
    """The header's figure is the route's, not this layer's. Recomputing it here
    is impossible anyway: this payload holds one page, and the count covers the
    whole filtered set."""
    _inject(
        monkeypatch, FakeClient(insights={**_INSIGHTS, "conversations": 41})
    )

    assert _get("/api/insights").json()["conversations"] == 41


def test_list_insights_without_a_count_reads_as_absent_not_zero(
    monkeypatch,
) -> None:
    """An agent deployed before the field sends none, and the client then
    renders no figure rather than a zero it would be stating as a fact."""
    _inject(monkeypatch, FakeClient(insights=_INSIGHTS))

    assert _get("/api/insights").json()["conversations"] is None


# --------------------------------------------------------------------------- #
# GET /api/insights/{insight_id}                                               #
# --------------------------------------------------------------------------- #

_INSIGHT_DETAIL = {
    "insight": {
        "insight_id": "ins-1",
        "label": "stopped early",
        "status": "NEW",
    },
    "occurrences": [
        {
            "occurrence_id": "occ-1",
            "run_id": "aaa11111",
            "label": "stopped early",
            "rubrics": [
                {
                    "rubric": {
                        "rubric_id": "1",
                        "expected_behavior": "completes the task",
                        "actual_behavior": "stopped after the first step",
                    },
                    "eval_case_id": "case-1",
                }
            ],
        }
    ],
    "next_page_token": None,
}


def test_get_insight_detail(monkeypatch) -> None:
    fake = FakeClient(insight_detail=_INSIGHT_DETAIL)
    _inject(monkeypatch, fake)
    r = _get("/api/insights/ins-1")
    assert r.status_code == 200
    body = r.json()
    assert body["insight"]["insight_id"] == "ins-1"
    assert body["occurrences"][0]["run_id"] == "aaa11111"
    # The panel asks for the rubric evidence without the heavy traces -- but it
    # does ask for the trajectories, which are the links out to Cloud Trace and
    # cost one extra query rather than a conversation apiece.
    assert fake.args_for("insights/get") == {
        "insight_id": "ins-1",
        "run_id": "",
        "page_token": "",
        "include_traces": False,
        "include_trajectories": True,
    }


def test_get_insight_traces_opt_in(monkeypatch) -> None:
    fake = FakeClient(insight_detail=_INSIGHT_DETAIL)
    _inject(monkeypatch, fake)
    _get("/api/insights/ins-1?includeTraces=1")
    assert fake.args_for("insights/get")["include_traces"] is True


def test_get_insight_missing_payload_errors(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(insight_detail=None))
    r = _get("/api/insights/nope")
    assert r.status_code == 200
    assert "error" in r.json()


# --------------------------------------------------------------------------- #
# The v2 dashboard's vocabulary                                                #
# --------------------------------------------------------------------------- #
# The replacement dashboard reads names our records do not use. These pin the
# reconciliation in `app.py`, and -- as much -- pin what it deliberately does
# not do: a field we have no value for stays absent, so the client's defaults
# decide how the gap reads instead of a zero standing in for a measurement.


def test_an_insight_with_no_root_cause_reports_no_diagnosis(
    monkeypatch,
) -> None:
    """`has_root_cause` states whether an insight has been diagnosed.

    Verifies that undiagnosed insights report has_root_cause as False with an
    empty diagnosis and without unneeded flags.
    """
    _inject(monkeypatch, FakeClient(insight_detail=_INSIGHT_DETAIL))
    body = _get("/api/insights/ins-1").json()
    assert body["insight"]["has_root_cause"] is False
    assert body["insight"]["diagnosis"] == ""
    assert "pre_dig" not in body["insight"]


def test_the_insight_list_is_marked_too(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(insights=_INSIGHTS))
    insights = _get("/api/insights").json()["insights"]
    assert insights
    assert all(i["has_root_cause"] is False for i in insights)
    assert all("pre_dig" not in i for i in insights)


def test_trajectory_ids_are_served_as_evidence_case_ids(monkeypatch) -> None:
    """#189 already writes the conversations behind a sighting. The dashboard
    calls that list something else; one column answers both."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {"occurrence_id": "o-1", "trajectory_ids": ["t-1", "t-2"]}
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    occurrence = _get("/api/insights/ins-1").json()["occurrences"][0]
    assert occurrence["evidence_case_ids"] == ["t-1", "t-2"]
    # Projected, not moved: the name we store under is still the one we store.
    assert occurrence["trajectory_ids"] == ["t-1", "t-2"]


def test_an_occurrence_with_no_trajectories_gets_an_empty_list(
    monkeypatch,
) -> None:
    """Not a missing key: the detail pane reads `.length` on it unguarded."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "o-1"}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert (
        _get("/api/insights/ins-1").json()["occurrences"][0][
            "evidence_case_ids"
        ]
        == []
    )


def test_a_conversation_is_served_with_its_console_link(monkeypatch) -> None:
    """The point of the whole chain: an occurrence names its conversations, and
    each one carries a way to open the trace behind it. Keyed by case id, so
    `evidence_case_ids` stays the panel's ordering and this only answers
    "where does this one go"."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "trajectory_ids": ["t-1"],
                "trajectories": [
                    {
                        "trajectory_id": "t-1",
                        "console_url": "https://console/x?tid=a",
                    }
                ],
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    occurrence = _get("/api/insights/ins-1").json()["occurrences"][0]
    assert occurrence["console_urls"] == {"t-1": "https://console/x?tid=a"}


def test_a_conversation_with_no_trace_gets_no_link(monkeypatch) -> None:
    """Every `big_query` trajectory, and anything sampled without a store.
    Absent rather than empty, so the panel renders a plain chip rather
    than an anchor to nowhere."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "trajectory_ids": ["t-1", "t-2"],
                "trajectories": [
                    {"trajectory_id": "t-1", "console_url": ""},
                    {
                        "trajectory_id": "t-2",
                        "console_url": "https://console/x?tid=b",
                    },
                ],
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    occurrence = _get("/api/insights/ins-1").json()["occurrences"][0]
    assert occurrence["console_urls"] == {"t-2": "https://console/x?tid=b"}
    assert occurrence["evidence_case_ids"] == ["t-1", "t-2"]


def test_an_engine_that_resolves_no_trajectories_still_serves_the_panel(
    monkeypatch,
) -> None:
    """A deployment whose trajectory table is missing, or occurrences older
    than the store. `console_urls` is a dict either way: the panel indexes it
    unguarded."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "o-1", "trajectory_ids": ["t-1"]}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert (
        _get("/api/insights/ins-1").json()["occurrences"][0]["console_urls"]
        == {}
    )


def test_an_engine_that_sends_its_own_evidence_case_ids_is_left_alone(
    monkeypatch,
) -> None:
    """So this adapter cannot overwrite a real dig-phase answer with the index
    of every trajectory in the cluster, which is a different, larger set."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "trajectory_ids": ["t-1", "t-2", "t-3"],
                "evidence_case_ids": ["t-2"],
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert _get("/api/insights/ins-1").json()["occurrences"][0][
        "evidence_case_ids"
    ] == ["t-2"]


def test_confidence_is_the_one_field_with_no_answer(monkeypatch) -> None:
    """Nothing in this pipeline measures a confidence, so none is sent.
    The dashboard renders no confidence percentage, avoiding displaying
    uncalculated or zeroed metric claims."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "o-1", "trace_count": 4}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()
    assert "confidence" not in body["occurrences"][0]
    assert "confidence" not in body["insight"]


def test_impact_is_the_traces_the_issue_touched(monkeypatch) -> None:
    """The dashboard sorts and ranks on `impact`, and nothing here ranks
    clusters. `trace_count` is already SUM(o.trace_count) over the sightings,
    which is the closest thing we have to how much an issue matters."""
    detail = {
        "insight": {"insight_id": "ins-1", "trace_count": 592},
        "occurrences": [],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert _get("/api/insights/ins-1").json()["insight"]["impact"] == 592


def test_cases_checked_is_the_traces_the_sighting_was_evaluated_over(
    monkeypatch,
) -> None:
    """The fork counts cases an independent Cloud Trace lookup confirmed. We run
    no second check, so the conversations the sighting was evaluated over is the
    honest number."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "o-1", "trace_count": 6}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert (
        _get("/api/insights/ins-1").json()["occurrences"][0]["cases_checked"]
        == 6
    )


def test_a_verification_explanation_becomes_the_diagnosis(monkeypatch) -> None:
    """`verify_clusters` writes what the defect is and why, and the sweep stores
    it under `analyses.verification`. The pane renders that explanation as the
    sighting's `diagnosis`.

    The fixture carries the verdict's `rationale` too, because a stored verdict
    does. The projection copies the map whole, so it reaches the client beside
    the explanation, and nothing renders it.
    """
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "analyses": {
                    "verification": {
                        "explanation": "The tool rejects any category outside its enum.",
                        "rationale": "Checked six traces; five carried a free-text value.",
                    }
                },
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()

    assert (
        body["occurrences"][0]["diagnosis"]
        == "The tool rejects any category outside its enum."
    )
    # And it lifts to the insight, which is what the header renders.
    assert body["insight"]["diagnosis"].startswith("The tool rejects")


def test_a_proposed_rename_reaches_the_pane_without_replacing_the_label(
    monkeypatch,
) -> None:
    """The pass proposes a sharper name when the stored label misleads. The
    pane shows it, and the stored label stays what later sweeps match on --
    renaming that would file one defect as two."""
    detail = {
        "insight": {
            "insight_id": "ins-1",
            "label": "handled the request poorly",
        },
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "analyses": {
                    "verification": {
                        "explanation": "The reply omitted the tracking number.",
                        "refined_label": "omitted the tracking number from order confirmations",
                    }
                },
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()

    assert (
        body["insight"]["refined_label"]
        == "omitted the tracking number from order confirmations"
    )
    assert body["insight"]["label"] == "handled the request poorly"


def test_the_verdicts_working_out_never_reaches_a_reader(monkeypatch) -> None:
    """The verdict's own bookkeeping stays out of the pane. A reader acts on the
    diagnosis and the label, and `valid` only ever reaches them as the absence
    of an insight, since an invalid candidate mints none."""
    detail = {
        "insight": {"insight_id": "ins-1", "label": "wrong start format"},
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "analyses": {
                    "verification": {
                        "explanation": "The booked start disagreed with the declaration.",
                        "valid": True,
                        "rationale": "The prompt demonstrates a wall-clock form the schema forbids.",
                    }
                },
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()

    # The diagnosis still reaches the pane; only the reasoning behind it does not.
    assert body["occurrences"][0]["diagnosis"].startswith("The booked start")
    assert "valid" not in body["occurrences"][0]
    assert "rationale" not in body["occurrences"][0]
    assert "valid" not in body["insight"]


def test_the_rationale_is_not_folded_into_the_diagnosis(monkeypatch) -> None:
    """It is the evidence for the verdict rather than the verdict, and the pane
    has one paragraph for this."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {
                "occurrence_id": "o-1",
                "analyses": {
                    "verification": {
                        "explanation": "Bad category.",
                        "rationale": "Trace-by-trace working.",
                    }
                },
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert (
        _get("/api/insights/ins-1").json()["occurrences"][0]["diagnosis"]
        == "Bad category."
    )


def test_the_verification_step_key_matches_the_agent(monkeypatch) -> None:
    """`ui/` cannot import the agent's packages, so the key is a hand-copy.
    Nothing but this holds the two level, and a rename would silently turn
    every diagnosis back into "undiagnosed"."""
    from ambient_quality_agent.tools.insights.models import (
        VERIFICATION_ANALYSIS_KEY,
        ClusterVerification,
    )

    assert ui_app.VERIFICATION_STEP == VERIFICATION_ANALYSIS_KEY
    # And the field the projection reads off it.
    assert "explanation" in ClusterVerification.model_fields


# --------------------------------------------------------------------------- #
# Recorded root causes                                                         #
# --------------------------------------------------------------------------- #
# Tests verifying recorded root causes, panel rendering, and filter forwarding.


def _record(**over: Any) -> dict[str, Any]:
    """Generate a sample root-cause record matching backend serialization.

    Args:
        **over: Field overrides for the record.

    Returns:
        Root-cause dictionary.
    """
    return {
        "root_cause_id": "rc-1",
        "insight_id": "ins-1",
        "occurrence_id": "occ-1",
        "agent_revision": "rev-9",
        "summary": "The refund step is phrased as a preference.",
        "edits": [
            {
                "path": "app/agent.py",
                "start_line": 42,
                "end_line": 44,
                "before": "old",
                "after": "new",
                "rationale": "Make it a constraint.",
            }
        ],
        "created_at": "2026-01-02T03:04:05Z",
        "edit_count": 1,
        **over,
    }


def test_a_sightings_root_cause_records_reach_the_panel(monkeypatch) -> None:
    """Verify structured root causes pass through intact for rich UI rendering."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "occ-1", "root_causes": [_record()]}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    occurrence = _get("/api/insights/ins-1").json()["occurrences"][0]

    assert occurrence["root_causes"] == [_record()]


def test_a_record_whose_sighting_is_off_the_page_still_reaches_the_panel(
    monkeypatch,
) -> None:
    """Verify root causes survive when their original occurrence ID is missing."""
    orphan = _record(occurrence_id="occ-gone")
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "occ-1"}],
        "root_causes": [orphan],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()

    assert body["root_causes"] == [orphan]
    assert body["occurrences"][0]["root_causes"] == []
    # Ensure insight-level flags indicate diagnosis even without occurrence match.
    assert body["insight"]["has_root_cause"] is True


def test_an_occurrence_with_no_records_carries_an_empty_list(
    monkeypatch,
) -> None:
    """Verify missing root causes default to empty arrays for safe UI access."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [{"occurrence_id": "occ-1"}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()
    assert body["occurrences"][0]["root_causes"] == []
    assert body["root_causes"] == []


def test_the_root_cause_summary_does_not_become_the_diagnosis(
    monkeypatch,
) -> None:
    """Verify root-cause summaries do not overwrite insight diagnoses or labels."""
    detail = {
        "insight": {"insight_id": "ins-1", "label": "skipped the refund"},
        "occurrences": [{"occurrence_id": "occ-1", "root_causes": [_record()]}],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    body = _get("/api/insights/ins-1").json()

    assert body["occurrences"][0]["diagnosis"] == ""
    assert body["insight"]["diagnosis"] == ""
    assert body["insight"]["label"] == "skipped the refund"
    # Confirm the record remains accessible and the insight is marked as diagnosed.
    assert body["occurrences"][0]["root_causes"] == [_record()]
    assert body["insight"]["has_root_cause"] is True


def test_the_verification_explanation_survives_a_recorded_root_cause(
    monkeypatch,
) -> None:
    """Verify autorater explanations remain intact alongside recorded root causes."""
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {
                "occurrence_id": "occ-1",
                "analyses": {"verification": {"explanation": "Bad category."}},
                "root_causes": [_record()],
            }
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    occurrence = _get("/api/insights/ins-1").json()["occurrences"][0]

    assert occurrence["diagnosis"] == "Bad category."
    assert occurrence["root_causes"][0]["summary"] == (
        "The refund step is phrased as a preference."
    )


def test_every_record_on_a_sighting_is_forwarded(monkeypatch) -> None:
    """Verify all root-cause records on a sighting are forwarded to the UI."""
    older = _record(created_at="2025-01-01T00:00:00Z", summary="Older.")
    newer = _record(created_at="2026-05-05T00:00:00Z", summary="Newer.")
    detail = {
        "insight": {"insight_id": "ins-1"},
        "occurrences": [
            {"occurrence_id": "occ-1", "root_causes": [older, newer]}
        ],
    }
    _inject(monkeypatch, FakeClient(insight_detail=detail))
    assert _get("/api/insights/ins-1").json()["occurrences"][0][
        "root_causes"
    ] == [
        older,
        newer,
    ]


def test_the_list_marks_a_diagnosed_insight(monkeypatch) -> None:
    """Verify list-level has_root_cause flags reach the card."""
    insights = {
        "insights": [{"insight_id": "ins-1", "has_root_cause": True}],
        "total": 1,
    }
    _inject(monkeypatch, FakeClient(insights=insights))
    insight = _get("/api/insights").json()["insights"][0]

    assert insight["has_root_cause"] is True


def test_an_undiagnosed_insight_stays_undiagnosed(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(insights=_INSIGHTS))
    insight = _get("/api/insights").json()["insights"][0]

    assert insight["has_root_cause"] is False


def test_the_negative_root_cause_filter_is_forwarded_as_a_string(
    monkeypatch,
) -> None:
    """Verify negative filters serialize as strings to prevent dropping falsy args."""
    fake = FakeClient(insights={"insights": [], "total": 0})
    _inject(monkeypatch, fake)

    _get("/api/insights?hasRootCause=false")

    assert fake.args_for("insights/list")["has_root_cause"] == "false"


def test_the_positive_root_cause_filter_is_forwarded(monkeypatch) -> None:
    fake = FakeClient(insights={"insights": [], "total": 0})
    _inject(monkeypatch, fake)

    _get("/api/insights?hasRootCause=true")

    assert fake.args_for("insights/list")["has_root_cause"] == "true"


def test_no_root_cause_filter_means_no_argument(monkeypatch) -> None:
    """Verify omitting filter parameter queries both diagnosed and undiagnosed."""
    fake = FakeClient(insights={"insights": [], "total": 0})
    _inject(monkeypatch, fake)

    _get("/api/insights")

    assert "has_root_cause" not in fake.args_for("insights/list")


def test_the_root_cause_filter_sentinel_matches_the_agent() -> None:
    """Verify UI filter sentinel parser matches agent backend parsing."""
    from ambient_quality_agent.tools.orchestrator.insight_tools import (
        _parse_tri_state,
    )

    for sentinel, expected in (("true", True), ("false", False), ("", None)):
        parsed, error = _parse_tri_state(sentinel, "has_root_cause")
        assert error is None
        assert parsed is expected


# --------------------------------------------------------------------------- #
# The goal and the memories                                                   #
# --------------------------------------------------------------------------- #
# Pass-throughs: the agent owns these documents, so what is pinned here is the
# relaying -- the route called, and how a refusal from the agent becomes a
# status code the dashboard can act on.


def test_the_goal_is_relayed_from_the_agent(monkeypatch) -> None:
    payload = {
        "goal": "Book travel.",
        "available": True,
        "uri": "gs://b/goal.md",
    }
    fake = FakeClient(documents={"documents/goal/get": payload})
    _inject(monkeypatch, fake)

    assert _get("/api/goal").json() == payload


def test_saving_the_goal_forwards_the_text(monkeypatch) -> None:
    fake = FakeClient(documents={"documents/goal/set": {"goal": "New goal."}})
    _inject(monkeypatch, fake)

    assert _put("/api/goal", json={"goal": "New goal."}).json() == {
        "goal": "New goal."
    }
    assert fake.args_for("documents/goal/set") == {"goal": "New goal."}


def test_a_refused_goal_is_a_400_not_a_500(monkeypatch) -> None:
    """The agent refuses a goal over 8 KiB. That is the caller's mistake, so it
    has to reach the dashboard as one -- a 500 would read as the service
    breaking."""
    fake = FakeClient(
        documents={"documents/goal/set": {"error": "longer than 8 KiB"}}
    )
    _inject(monkeypatch, fake)

    response = _put("/api/goal", json={"goal": "x" * 9000})
    assert response.status_code == 400
    assert response.json()["error"] == "longer than 8 KiB"


def test_a_goal_read_that_fails_is_reported_not_drawn_as_unset(
    monkeypatch,
) -> None:
    """ "No goal set" and "we could not look" are different, and the card says
    something different for each."""
    _inject(monkeypatch, BoomClient())

    body = _get("/api/goal").json()
    assert body["available"] is False
    assert body["goal"] is None
    assert body["reason"]


def test_goal_versions_are_relayed_from_the_agent(monkeypatch) -> None:
    payload = {
        "versions": [
            {
                "version": "0123456789ab",
                "text": "Book travel.",
                "created_at": "2026-09-01T09:00:00+00:00",
                "last_activated_at": "2026-09-01T09:00:00+00:00",
                "active": True,
            }
        ],
        "available": True,
    }
    _inject(
        monkeypatch, FakeClient(documents={"documents/goal/versions": payload})
    )

    assert _get("/api/goal/versions").json() == payload


def test_goal_versions_that_cannot_be_read_are_reported(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())

    body = _get("/api/goal/versions").json()
    assert body["available"] is False
    assert body["versions"] == []
    assert body["reason"]


def test_memories_are_relayed_from_the_agent(monkeypatch) -> None:
    payload = {
        "memories": [
            {
                "id": "abc",
                "date": "2026-09-01",
                "source": "run",
                "text": "A rule.",
            }
        ],
        "available": True,
        "uri": "gs://b/memories/",
    }
    _inject(
        monkeypatch, FakeClient(documents={"documents/memories/list": payload})
    )

    assert _get("/api/memories").json() == payload


def test_deleting_a_memory_forwards_the_id(monkeypatch) -> None:
    fake = FakeClient(
        documents={"documents/memories/delete": {"deleted": True}}
    )
    _inject(monkeypatch, fake)

    assert _delete("/api/memories/abc123").json() == {"deleted": True}
    assert fake.args_for("documents/memories/delete") == {"memory_id": "abc123"}


def test_deleting_an_unknown_memory_is_a_404(monkeypatch) -> None:
    fake = FakeClient(
        documents={
            "documents/memories/delete": {"error": "No memory with id 'x'."}
        }
    )
    _inject(monkeypatch, fake)

    assert _delete("/api/memories/x").status_code == 404


def test_a_memories_read_that_fails_is_not_an_empty_list(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())

    body = _get("/api/memories").json()
    assert body["available"] is False
    assert body["memories"] == []


# --------------------------------------------------------------------------- #
# Ambient health                                                               #
# --------------------------------------------------------------------------- #


def test_health_is_relayed_from_the_agent(monkeypatch) -> None:
    payload = {
        "verdict": "watching",
        "reason": "Sweeps arriving.",
        "open_findings": 0,
    }
    _inject(monkeypatch, FakeClient(health=payload))

    assert _get("/api/health").json() == payload


def test_an_unreachable_health_check_is_an_error_not_a_verdict(
    monkeypatch,
) -> None:
    """`stat-bar.tsx` draws "Health unavailable" for an `error`. A verdict here
    would be the badge asserting the loop is fine because we could not look."""
    _inject(monkeypatch, BoomClient())

    body = _get("/api/health").json()
    assert "error" in body
    assert "verdict" not in body


def test_an_empty_health_payload_is_reported_too(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(health={}))
    assert "error" in _get("/api/health").json()


# --------------------------------------------------------------------------- #
# GET /api/source -- the surface nothing publishes yet                          #
# --------------------------------------------------------------------------- #


def test_the_source_card_is_told_why_it_is_empty(monkeypatch) -> None:
    """`source-card.tsx` renders `reason` verbatim in amber, so the agent's
    wording is the UI copy and has to survive the relay unchanged."""
    payload = {
        "available": False,
        "reason": "No source snapshot has been published.",
    }
    _inject(monkeypatch, FakeClient(unpublished={"source": payload}))

    assert _get("/api/source").json() == payload


def test_an_unreachable_source_read_is_still_an_unavailable_card(
    monkeypatch,
) -> None:
    """Not an `error`: the card asks whether findings can cite code, and a read
    that failed and a snapshot that was never published are both "no"."""
    _inject(monkeypatch, BoomClient())

    body = _get("/api/source").json()
    assert body["available"] is False
    assert body["reason"]


# --------------------------------------------------------------------------- #
# GET /api/investigations/{run}/cases/{case}                                    #
# --------------------------------------------------------------------------- #


def test_a_case_conversation_is_relayed(monkeypatch) -> None:
    payload = {
        "case": {"trajectory_id": "case-1", "status": "ingested", "turns": []}
    }
    _inject(monkeypatch, FakeClient(case=payload))

    assert _get("/api/investigations/run-1/cases/case-1").json() == payload


def test_the_case_route_forwards_both_ids(monkeypatch) -> None:
    """The case id is the trajectory id -- an occurrence's `evidence_case_ids`
    are projected from `trajectory_ids`, so no lookup stands between them."""
    client = FakeClient(case={"case": {}})
    _inject(monkeypatch, client)

    _get("/api/investigations/run-42/cases/case-7")

    assert client.args_for("trajectories/case") == {
        "trajectory_id": "case-7",
        "run_id": "run-42",
    }


def test_an_unreachable_case_read_is_an_error(monkeypatch) -> None:
    """Unlike the source card, an empty conversation here is a claim about what
    the agent did. A failed read has to say it failed."""
    _inject(monkeypatch, BoomClient())

    assert "error" in _get("/api/investigations/run-1/cases/case-1").json()


def test_an_empty_case_payload_is_reported_too(monkeypatch) -> None:
    _inject(monkeypatch, FakeClient(case={}))

    assert "error" in _get("/api/investigations/run-1/cases/case-1").json()


# --------------------------------------------------------------------------- #
# POST /api/insights/{id}/dismiss and /merge                                   #
# --------------------------------------------------------------------------- #


def test_dismiss_forwards_the_id_and_answers_the_one_key_the_client_reads(
    monkeypatch,
) -> None:
    fake = FakeClient()
    _inject(monkeypatch, fake)

    r = _post("/api/insights/ins-1/dismiss")

    assert r.status_code == 200
    assert r.json() == {"dismissed": True}
    assert fake.args_for("insights/dismiss") == {"insight_id": "ins-1"}


def test_dismiss_sends_no_body_at_all(monkeypatch) -> None:
    """The vendored client posts without one -- not an empty object -- so a route
    that read the body would 422 every Dismiss click."""
    _inject(monkeypatch, FakeClient())

    assert _post("/api/insights/ins-1/dismiss").status_code == 200


def test_dismissing_an_unknown_insight_is_a_404(monkeypatch) -> None:
    _inject(
        monkeypatch,
        FakeClient(
            insight_actions={"insights/dismiss": {"error": "No insight found."}}
        ),
    )

    r = _post("/api/insights/nope/dismiss")

    assert r.status_code == 404
    assert "No insight found." in r.json()["error"]


def test_dismiss_degrades_when_the_agent_is_unreachable(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())

    r = _post("/api/insights/ins-1/dismiss")

    assert r.status_code == 502
    assert "backend exploded" in r.json()["error"]


def test_merge_forwards_the_path_id_and_the_target_from_the_body(
    monkeypatch,
) -> None:
    fake = FakeClient()
    _inject(monkeypatch, fake)

    r = _post("/api/insights/ins-1/merge", json={"target_insight_id": "ins-2"})

    assert r.status_code == 200
    assert r.json() == {"merged": True}
    assert fake.args_for("insights/merge") == {
        "insight_id": "ins-1",
        "target_insight_id": "ins-2",
    }


def test_merge_without_a_target_never_reaches_the_agent(monkeypatch) -> None:
    fake = FakeClient()
    _inject(monkeypatch, fake)

    r = _post("/api/insights/ins-1/merge", json={})

    assert r.status_code == 400
    assert fake.calls == []


def test_a_refused_merge_reads_as_the_callers_mistake(monkeypatch) -> None:
    """Both refusals are about which insights were named, so 400 rather than
    502: nothing here failed."""
    _inject(
        monkeypatch,
        FakeClient(
            insight_actions={
                "insights/merge": {"error": "target is a duplicate"}
            }
        ),
    )

    r = _post("/api/insights/ins-1/merge", json={"target_insight_id": "ins-2"})

    assert r.status_code == 400
    assert "target is a duplicate" in r.json()["error"]


# --------------------------------------------------------------------------- #
# An engine with no agent on it                                                #
# --------------------------------------------------------------------------- #


class PlaceholderClient:
    """Stub standing in for the engine before `agents-cli deploy` has run.

    Terraform creates the Agent Runtime agent with a hello-world placeholder
    image, which answers the command routes with a body that is not JSON; the
    client's `r.json()` raises exactly this.
    """

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise json.JSONDecodeError("Expecting value", "", 0)


class UnavailableClient:
    """Stub standing in for the platform mid-revision-swap."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = httpx.Request(
            "POST", "http://engine/api/investigations/daily"
        )
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            "upstream", request=request, response=response
        )


def test_a_placeholder_engine_is_reported_as_a_deployment_not_a_parser_error(
    monkeypatch,
) -> None:
    """`JSONDecodeError: Expecting value: line 1 column 1 (char 0)` names the
    parser rather than the situation, and reads as a broken deployment where
    the truth is an unfinished one."""
    _inject(monkeypatch, PlaceholderClient())

    body = _get("/api/daily").json()

    assert body["days"] == []
    assert body["error"] == ui_app._NOT_DEPLOYED
    assert "JSONDecodeError" not in body["error"]


def test_every_panel_says_the_same_thing_about_it(monkeypatch) -> None:
    """The dashboard draws these side by side, so one window of the same cause
    must not read as several unrelated failures."""
    _inject(monkeypatch, PlaceholderClient())

    assert _get("/api/config").json()["error"] == ui_app._NOT_DEPLOYED
    assert _get("/api/health").json()["error"] == ui_app._NOT_DEPLOYED
    assert _get("/api/stats").json()["error"] == ui_app._NOT_DEPLOYED
    assert _get("/api/investigations").json()["error"] == ui_app._NOT_DEPLOYED
    assert _get("/api/insights").json()["error"] == ui_app._NOT_DEPLOYED
    assert _get("/api/source").json()["reason"] == ui_app._NOT_DEPLOYED
    assert _get("/api/goal").json()["reason"] == ui_app._NOT_DEPLOYED
    assert _get("/api/memories").json()["reason"] == ui_app._NOT_DEPLOYED


def test_a_gateway_status_while_a_revision_swaps_reads_the_same_way(
    monkeypatch,
) -> None:
    """Nothing is serving the routes yet, which is the one thing the reader
    needs to know -- whether the platform says so with an empty body or with a
    gateway status."""
    for status in (502, 503, 504):
        _inject(monkeypatch, UnavailableClient(status))

        assert _get("/api/daily").json()["error"] == ui_app._NOT_DEPLOYED


def test_a_404_is_still_reported_verbatim(monkeypatch) -> None:
    """The agent's own FastAPI answers 404 for a route it does not carry. That
    is a version skew between this image and the engine's, and calling it a
    rollout in progress sends the reader off to wait for one that finished."""
    _inject(monkeypatch, UnavailableClient(404))

    error = _get("/api/daily").json()["error"]

    assert error.startswith("HTTPStatusError:")
    assert error != ui_app._NOT_DEPLOYED


def test_an_ordinary_backend_failure_is_untouched(monkeypatch) -> None:
    _inject(monkeypatch, BoomClient())

    assert (
        _get("/api/daily").json()["error"] == "RuntimeError: backend exploded"
    )


def test_the_message_is_the_one_the_dashboard_draws_amber() -> None:
    """Pins the wording to `NOT_DEPLOYED_RE` in `ui/web/lib/failure.ts`.

    The dashboard tells an unfinished deployment from a broken one by matching
    this text, and shows the first sentence alone where a pane has no room for
    the rest. Reworded here, the banner silently goes back to red.
    """
    assert "is not serving its API yet" in ui_app._NOT_DEPLOYED
    first, _, rest = ui_app._NOT_DEPLOYED.partition(". ")
    assert first.endswith("yet") and rest
