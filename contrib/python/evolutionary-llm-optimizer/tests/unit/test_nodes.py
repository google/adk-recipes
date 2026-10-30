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
"""Unit tests for the workflow nodes and graph routing. No model calls."""

import asyncio
from types import SimpleNamespace

import pytest
from google.adk import Workflow
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import FunctionNode
from google.genai import types

from app import problem
from app.agent import (
    CONTINUE,
    DONE,
    INVALID,
    PROPOSE,
    Candidate,
    build_report,
    create_workflow,
    deliver,
    format_bytes,
    init,
    reject,
    render_report,
    report,
    select,
    verify,
)

# Same result as the bundled station_demand baseline, with a range predicate
# on the raw start_date (row groups can be skipped) and no SELECT *.
STATION_DEMAND_REWRITE = """\
SELECT start_station_name, COUNT(*) AS hires,
       ROUND(AVG(duration) / 60.0, 1) AS avg_minutes
FROM cycle_hire
WHERE start_date >= TIMESTAMP '2016-04-01'
  AND start_date < TIMESTAMP '2016-05-01'
GROUP BY start_station_name
ORDER BY hires DESC, start_station_name
LIMIT 10"""

WRONG_RESULT = STATION_DEMAND_REWRITE.replace("LIMIT 10", "LIMIT 9")
NOT_A_SELECT = "DELETE FROM cycle_hire"
MESSAGE = "Cut the bytes scanned by the station demand query."


@pytest.fixture
def ctx() -> SimpleNamespace:
    """Stands in for google.adk.Context: function nodes only use ctx.state."""
    return SimpleNamespace(state={})


@pytest.fixture(autouse=True)
def _small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_EVALUATIONS", "2")
    monkeypatch.setenv("POPULATION_SIZE", "3")
    monkeypatch.setenv("QUERY_ENGINE", "duckdb")


def _verify_and_select(ctx: SimpleNamespace, idea: str, sql: str):
    checked = verify(ctx, Candidate(idea=idea, sql=sql))
    return checked, select(ctx, checked.output)


def test_format_bytes() -> None:
    assert format_bytes(999) == "999 B"
    assert format_bytes(234_600) == "234.60 KB"
    assert format_bytes(1_342_803) == "1.34 MB"
    assert format_bytes(None) is None


def test_init_runs_baseline_and_seeds_state(ctx: SimpleNamespace) -> None:
    event = init(ctx, MESSAGE)
    assert event.actions.route == PROPOSE
    brief = event.output
    assert brief["query"]["id"] == "station_demand"
    assert brief["baseline_bytes_scanned"] > 0
    assert brief["best_bytes_scanned"] == brief["baseline_bytes_scanned"]
    assert brief["expected_result_preview"]["row_count"] == 10
    assert brief["evaluations_remaining"] == 2
    assert brief["operator_request"] == MESSAGE
    assert brief["table"] == "cycle_hire"
    assert [p["id"] for p in brief["parents"]] == [0]
    assert brief["last_feedback"] is None
    assert "Baseline scans" in event.content.parts[0].text


def test_init_routes_invalid_custom_sql_to_reject(ctx: SimpleNamespace) -> None:
    event = init(ctx, "```sql\nSELECT nope FROM cycle_hire\n```")
    assert event.actions.route == INVALID
    text = reject(event.output).output
    assert "could not run" in text and "nope" in text


def test_init_resets_a_previous_run(ctx: SimpleNamespace) -> None:
    init(ctx, MESSAGE)
    _verify_and_select(ctx, "range filter", STATION_DEMAND_REWRITE)
    init(ctx, MESSAGE)
    assert ctx.state["evolve_evaluations"] == 0
    assert len(ctx.state["evolve_population"]) == 1
    assert len(ctx.state["evolve_history"]) == 1


def test_cheaper_equivalent_rewrite_becomes_best(ctx: SimpleNamespace) -> None:
    init(ctx, MESSAGE)
    checked, routed = _verify_and_select(
        ctx, "range filter", STATION_DEMAND_REWRITE
    )
    assert checked.output["valid"], checked.output
    assert routed.actions.route == CONTINUE
    brief = routed.output
    assert brief["best_bytes_scanned"] == checked.output["bytes_scanned"]
    assert brief["best_bytes_scanned"] < brief["baseline_bytes_scanned"]
    assert brief["parents"][0]["sql"] == STATION_DEMAND_REWRITE
    assert brief["last_feedback"]["id"] == 1


def test_wrong_result_is_rejected_with_reason(ctx: SimpleNamespace) -> None:
    init(ctx, MESSAGE)
    checked, routed = _verify_and_select(ctx, "drop a row", WRONG_RESULT)
    assert not checked.output["valid"]
    assert (
        "wrong result: returns 9 rows, expected 10" in checked.output["error"]
    )
    assert len(ctx.state["evolve_population"]) == 1
    assert routed.output["last_feedback"]["valid"] is False


def test_non_select_is_rejected(ctx: SimpleNamespace) -> None:
    init(ctx, MESSAGE)
    checked = verify(ctx, Candidate(idea="bad", sql=NOT_A_SELECT))
    assert checked.output["error"] == "must be a SELECT, got DELETE"


def test_duplicate_sql_is_rejected_without_running(
    ctx: SimpleNamespace,
) -> None:
    init(ctx, MESSAGE)
    baseline = ctx.state["evolve_query"]["sql"]
    checked = verify(ctx, Candidate(idea="same", sql="  " + baseline + " ;"))
    assert "identical to candidate 0" in checked.output["error"]


def test_budget_exhaustion_routes_to_report(ctx: SimpleNamespace) -> None:
    init(ctx, MESSAGE)
    _, first = _verify_and_select(ctx, "bad", NOT_A_SELECT)
    _, second = _verify_and_select(ctx, "range", STATION_DEMAND_REWRITE)
    assert first.actions.route == CONTINUE
    assert second.actions.route == DONE
    assert second.output["best_id"] == 2


def test_report_uses_verified_numbers(ctx: SimpleNamespace) -> None:
    init(ctx, MESSAGE)
    checked, _ = _verify_and_select(ctx, "range", STATION_DEMAND_REWRITE)
    numbers = build_report(ctx.state)
    assert numbers["best_bytes_scanned"] == checked.output["bytes_scanned"]
    assert numbers["bytes_saved"] > 0
    assert 0 < numbers["reduction_percent"] < 100
    text = render_report(numbers)
    assert str(format_bytes(numbers["bytes_saved"])) in text
    assert "```sql" in text
    event = report(ctx)
    assert "best_sql" not in event.output
    assert event.output["bytes_saved"] == format_bytes(numbers["bytes_saved"])
    final = deliver(ctx, "### Summary\nShort.")
    assert final.output.startswith(ctx.state["evolve_report"])
    assert ctx.state["evolve_report"].startswith("## SQL cost report: ")
    assert final.output.endswith("### Summary\nShort.")


# --- workflow graph ---------------------------------------------------------


def _stub_workflow(sqls: list[str], seen: list[dict]) -> Workflow:
    def propose(node_input: dict) -> dict:
        seen.append(node_input)
        return {"idea": f"idea {len(seen)}", "sql": sqls[len(seen) - 1]}

    def narrate(node_input: dict) -> str:
        return f"saved {node_input['bytes_saved']}"

    return create_workflow(
        proposer=FunctionNode(func=propose, name="propose"),
        narrator=FunctionNode(func=narrate, name="narrate"),
    )


async def _run(workflow: Workflow, text: str) -> tuple[list, dict]:
    runner = InMemoryRunner(app=App(root_agent=workflow, name="app"))
    session = await runner.session_service.create_session(
        app_name="app", user_id="u"
    )
    message = types.Content(role="user", parts=[types.Part(text=text)])
    events = [
        e
        async for e in runner.run_async(
            user_id="u", session_id=session.id, new_message=message
        )
    ]
    session = await runner.session_service.get_session(
        app_name="app", user_id="u", session_id=session.id
    )
    assert session is not None
    return events, session.state


def test_workflow_loops_until_budget_then_reports() -> None:
    seen: list[dict] = []
    events, state = asyncio.run(
        _run(
            _stub_workflow([WRONG_RESULT, STATION_DEMAND_REWRITE], seen),
            MESSAGE,
        )
    )
    assert len(seen) == 2
    assert seen[1]["last_feedback"]["valid"] is False
    routes = [e.actions.route for e in events if e.actions.route]
    assert routes == [PROPOSE, CONTINUE, DONE]
    assert state["evolve_evaluations"] == 2
    final = [e for e in events if e.output is not None][-1]
    assert final.output.startswith("## SQL cost report: ")
    saved = format_bytes(build_report(state)["bytes_saved"])
    assert final.output.endswith(f"saved {saved}")


def test_workflow_rejects_a_baseline_that_cannot_run() -> None:
    events, _ = asyncio.run(
        _run(_stub_workflow([], []), "```sql\nDROP VIEW cycle_hire\n```")
    )
    routes = [e.actions.route for e in events if e.actions.route]
    assert routes == [INVALID]
    final = [e for e in events if e.output is not None][-1]
    assert "must be a SELECT" in final.output


def test_bundled_queries_select_by_message() -> None:
    for query in problem.QUERIES:
        chosen = problem.select_query(
            f"please optimize {query['id']}", "duckdb", "cycle_hire"
        )
        assert chosen["id"] == query["id"]
    assert (
        problem.select_query("hello", "duckdb", "cycle_hire")["id"]
        == problem.QUERIES[0]["id"]
    )
