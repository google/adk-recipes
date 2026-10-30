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
"""An evolutionary LLM optimizer for SQL cost, built as an ADK 2.0 workflow.

Applied to a real data-platform task: rewriting an expensive analytics query
so it returns exactly the same result while scanning fewer bytes, the unit
BigQuery on-demand pricing bills. The model only proposes rewrites; a
deterministic verifier runs each one, proves it returns the baseline's
result, and meters the bytes it scans::

    START -> init --propose--> propose -> verify -> select --continue--> propose
               |                                     |
               +--invalid--> reject                  +--done--> report
                                                                  |
                                                    narrate <-----+
                                                       |
                                                    deliver

- init (function): picks the baseline query from the message (a bundled
  query or a pasted ```sql block), runs it, and seeds the population, the
  expected result and the evaluation budget in session state. Routes to
  `reject` if the baseline itself cannot run.
- propose (single_turn Agent): writes ONE rewrite from the query, the best
  verified parents and the last verifier feedback.
- verify (function): checks the rewrite is a single read-only SELECT, runs it
  on the query engine, compares the result with the baseline's and records
  bytes scanned and runtime. No model involved.
- select (function): updates the population and routes to another proposal
  or to the report, bounded by MAX_EVALUATIONS.
- report (function): re-runs the baseline and the best rewrite and builds the
  cost report from verifier numbers only.
- narrate (single_turn Agent): adds a short summary for the data team,
  quoting only numbers that are in the report.
- deliver (function): sends the report and the summary as one final answer.
"""

import functools
import hashlib
import os
import re
from typing import Any

from google.adk import Agent, Context, Event, Workflow
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.workflow import BaseNode
from google.genai import types
from pydantic import BaseModel, Field

from . import population as pop
from . import problem
from .engines import Engine, create_engine

_POPULATION_KEY = "evolve_population"
_HISTORY_KEY = "evolve_history"
_EVALUATIONS_KEY = "evolve_evaluations"
_REQUEST_KEY = "evolve_request"
_SEEN_KEY = "evolve_seen_sql"
_QUERY_KEY = "evolve_query"
_BASELINE_KEY = "evolve_baseline"
_REPORT_KEY = "evolve_report"

CONTINUE = "continue"
DONE = "done"
PROPOSE = "propose"
INVALID = "invalid"

# Long verifier errors are trimmed before they go back to the model.
_MAX_ERROR_CHARS = 400


def _required_env(name: str, value: str | None) -> str:
    """Validates a setting declared in .env.example, failing clearly if unset."""
    if not value:
        raise RuntimeError(f"{name} is not set; copy .env.example to .env.")
    return value


def _max_evaluations() -> int:
    return int(_required_env("MAX_EVALUATIONS", os.getenv("MAX_EVALUATIONS")))


def _population_size() -> int:
    return int(_required_env("POPULATION_SIZE", os.getenv("POPULATION_SIZE")))


def _timeout_seconds() -> float:
    return float(
        _required_env(
            "CANDIDATE_TIMEOUT_SECONDS", os.getenv("CANDIDATE_TIMEOUT_SECONDS")
        )
    )


def _query_engine() -> str:
    return _required_env("QUERY_ENGINE", os.getenv("QUERY_ENGINE"))


def _max_bytes_billed() -> int | None:
    value = os.getenv("BIGQUERY_MAX_BYTES_BILLED")
    return int(value) if value else None


@functools.cache
def _cached_engine(
    engine: str,
    timeout: float,
    table: str | None,
    project: str | None,
    max_bytes_billed: int | None,
) -> Engine:
    return create_engine(
        engine,
        timeout_seconds=timeout,
        bigquery_table=table,
        bigquery_project=project,
        max_bytes_billed=max_bytes_billed,
    )


def get_engine() -> Engine:
    """Returns the query engine configured by QUERY_ENGINE."""
    return _cached_engine(
        _query_engine(),
        _timeout_seconds(),
        os.getenv("BIGQUERY_TABLE"),
        os.getenv("GOOGLE_CLOUD_PROJECT"),
        _max_bytes_billed(),
    )


def _sql_hash(sql: str) -> str:
    canonical = " ".join(sql.strip().rstrip(";").split()).lower()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tiebreak(runtime_ms: float) -> float:
    # Whole milliseconds, so timer noise does not reorder equal-byte rewrites.
    return float(round(runtime_ms))


def format_bytes(count: int | None) -> str | None:
    """Formats a byte count the way the report shows it."""
    if count is None:
        return None
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return (
                f"{value:,.0f} {unit}"
                if unit == "B"
                else f"{value:,.2f} {unit}"
            )
        value /= 1000
    return None  # pragma: no cover - unreachable


# ---------------------------------------------------------------------------
# Schemas for the data that flows between nodes
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """One rewrite proposed by the model."""

    idea: str = Field(
        description=(
            "One sentence: what changed versus the parent and why it should"
            " scan fewer bytes."
        )
    )
    sql: str = Field(
        description=(
            "The complete rewritten query: one read-only SELECT (WITH is fine)"
            " in the given dialect, against the same table."
        )
    )


class ProposalBrief(BaseModel):
    """Everything the proposer sees for one round. Built by init and select."""

    problem: str
    dialect: str
    engine_notes: str
    table: str
    schema_: list[dict[str, str]] = Field(alias="schema")
    query: dict[str, Any] = Field(
        description="Title, business question and known anti-pattern."
    )
    baseline_sql: str
    baseline_bytes_scanned: int
    baseline_runtime_ms: float
    expected_result_preview: dict[str, Any] = Field(
        description="Columns and the first rows every rewrite must return."
    )
    best_bytes_scanned: int
    parents: list[dict[str, Any]] = Field(
        description="Best verified rewrites so far, cheapest first, with SQL."
    )
    tried: list[dict[str, Any]] = Field(
        description="Every rewrite tried so far with its verified result."
    )
    last_feedback: dict[str, Any] | None = Field(
        default=None,
        description="Verifier result for the latest rewrite, if any.",
    )
    evaluations_remaining: int
    max_sql_chars: int = Field(
        description="Rewrites longer than this are rejected unrun."
    )
    operator_request: str = ""

    model_config = {"populate_by_name": True}


def _brief(state: Any) -> dict[str, Any]:
    """Builds the proposer's input from session state."""
    engine = get_engine()
    query = state[_QUERY_KEY]
    baseline = state[_BASELINE_KEY]
    population = sorted(state.get(_POPULATION_KEY, []), key=pop.rank_key)
    history = state.get(_HISTORY_KEY, [])
    last = history[-1] if len(history) > 1 else None
    return ProposalBrief(
        problem=problem.PROBLEM_STATEMENT,
        dialect=engine.dialect,
        engine_notes=engine.notes,
        table=engine.table,
        schema=problem.SCHEMA,
        query={k: query[k] for k in ("id", "title", "question", "ordered")},
        baseline_sql=query["sql"],
        baseline_bytes_scanned=baseline["bytes_scanned"],
        baseline_runtime_ms=baseline["runtime_ms"],
        expected_result_preview={
            "columns": baseline["columns"],
            "first_rows": baseline["rows"][:3],
            "row_count": len(baseline["rows"]),
        },
        best_bytes_scanned=population[0]["score"],
        parents=[
            {
                "id": c["id"],
                "bytes_scanned": c["score"],
                "runtime_ms": c["runtime_ms"],
                "idea": c["idea"],
                "sql": c["sql"],
            }
            for c in population
        ],
        tried=[
            {k: h[k] for k in ("id", "idea", "valid", "bytes_scanned", "error")}
            for h in history
        ],
        last_feedback=last,
        evaluations_remaining=_max_evaluations()
        - state.get(_EVALUATIONS_KEY, 0),
        max_sql_chars=problem.MAX_SQL_CHARS,
        operator_request=state.get(_REQUEST_KEY, ""),
    ).model_dump(by_alias=True)


def _history_entry(
    candidate_id: int, idea: str, sql: str, result: dict[str, Any]
) -> dict[str, Any]:
    error = result.get("error")
    return {
        "id": candidate_id,
        "idea": idea,
        "sql_chars": len(sql),
        "valid": bool(result["valid"]),
        "bytes_scanned": result.get("bytes_scanned")
        if result["valid"]
        else None,
        "runtime_ms": result.get("runtime_ms") if result["valid"] else None,
        "error": error[:_MAX_ERROR_CHARS] if error else None,
    }


def verify_sql(
    sql: str, baseline: dict[str, Any], ordered: bool
) -> dict[str, Any]:
    """Runs a rewrite and proves it returns the baseline's result."""
    if len(sql) > problem.MAX_SQL_CHARS:
        return {
            "valid": False,
            "error": f"SQL exceeds {problem.MAX_SQL_CHARS} characters",
        }
    run = get_engine().run(sql)
    if not run["ok"]:
        return {"valid": False, "error": run["error"]}
    mismatch = problem.compare_results(baseline["rows"], run["rows"], ordered)
    if mismatch:
        return {"valid": False, "error": f"wrong result: {mismatch}"}
    return {
        "valid": True,
        "bytes_scanned": run["bytes_scanned"],
        "runtime_ms": run["runtime_ms"],
        "error": None,
    }


# ---------------------------------------------------------------------------
# Function nodes (deterministic, no model calls)
# ---------------------------------------------------------------------------


def init(ctx: Context, node_input: str = "") -> Event:
    """Starts a fresh run: picks and runs the baseline, sets the budget.

    Every user message starts a new optimization run, so state from an
    earlier run in the same session is replaced.
    """
    engine = get_engine()
    message = (node_input or "").strip()
    query = problem.select_query(message, engine.name, engine.table)
    baseline = engine.run(query["sql"])
    if not baseline["ok"]:
        return Event(
            route=INVALID,
            output={"query": query, "error": baseline["error"]},
        )
    ctx.state[_QUERY_KEY] = query
    ctx.state[_BASELINE_KEY] = {
        k: baseline[k]
        for k in ("bytes_scanned", "runtime_ms", "columns", "rows")
    }
    idea = f"baseline: {query['anti_pattern']}"
    ctx.state[_POPULATION_KEY] = [
        {
            "id": 0,
            "idea": idea,
            "sql": query["sql"],
            "valid": True,
            "score": baseline["bytes_scanned"],
            "runtime_ms": baseline["runtime_ms"],
            "tiebreak": _tiebreak(baseline["runtime_ms"]),
        }
    ]
    ctx.state[_SEEN_KEY] = {_sql_hash(query["sql"]): 0}
    ctx.state[_HISTORY_KEY] = [
        _history_entry(0, idea, query["sql"], {**baseline, "valid": True})
    ]
    ctx.state[_EVALUATIONS_KEY] = 0
    ctx.state[_REQUEST_KEY] = message[:1000]
    return Event(
        route=PROPOSE,
        message=(
            f'Optimizing "{query["title"]}" on {engine.name}. Baseline scans'
            f" {format_bytes(baseline['bytes_scanned'])} in"
            f" {baseline['runtime_ms']:.1f} ms and returns"
            f" {len(baseline['rows'])} rows. Budget:"
            f" {_max_evaluations()} verified rewrites."
        ),
        output=_brief(ctx.state),
    )


def reject(node_input: dict[str, Any]) -> Event:
    """Explains why a run could not start (the baseline itself failed)."""
    text = (
        f'The baseline query "{node_input["query"]["title"]}" could not run,'
        f" so there is nothing to optimize: {node_input['error']}\n\n"
        "Paste one read-only SELECT in a ```sql block against the"
        " `cycle_hire` table, or ask for one of the bundled queries: "
        + ", ".join(f"`{q['id']}`" for q in problem.QUERIES)
        + "."
    )
    return Event(message=text, output=text)


def verify(ctx: Context, node_input: Candidate) -> Event:
    """Verifies one proposed rewrite and records the result."""
    used = ctx.state.get(_EVALUATIONS_KEY, 0) + 1
    ctx.state[_EVALUATIONS_KEY] = used
    history = list(ctx.state.get(_HISTORY_KEY, []))
    candidate_id = len(history)
    sql = node_input.sql.strip().rstrip(";").strip()
    sql_hash = _sql_hash(sql)
    seen = dict(ctx.state.get(_SEEN_KEY, {}))
    duplicate = seen.get(sql_hash)
    if duplicate is not None:
        result: dict[str, Any] = {
            "valid": False,
            "error": f"identical to candidate {duplicate}; propose a change",
        }
    else:
        result = verify_sql(
            sql, ctx.state[_BASELINE_KEY], ctx.state[_QUERY_KEY]["ordered"]
        )
    entry = _history_entry(candidate_id, node_input.idea, sql, result)
    ctx.state[_HISTORY_KEY] = [*history, entry]
    if duplicate is None:
        ctx.state[_SEEN_KEY] = {**seen, sql_hash: candidate_id}
    status = (
        f"{format_bytes(entry['bytes_scanned'])} scanned,"
        f" {entry['runtime_ms']:.1f} ms"
        if entry["valid"]
        else f"rejected ({entry['error']})"
    )
    return Event(
        message=f"Rewrite {candidate_id} ({used}/{_max_evaluations()}): "
        f"{node_input.idea} -> {status}",
        output={**entry, "sql": sql},
    )


def select(ctx: Context, node_input: dict[str, Any]) -> Event:
    """Updates the population and routes to another proposal or the report."""
    if node_input["valid"]:
        candidate = {
            "id": node_input["id"],
            "idea": node_input["idea"],
            "sql": node_input["sql"],
            "valid": True,
            "score": node_input["bytes_scanned"],
            "runtime_ms": node_input["runtime_ms"],
            "tiebreak": _tiebreak(node_input["runtime_ms"]),
        }
        ctx.state[_POPULATION_KEY] = pop.add_candidate(
            ctx.state.get(_POPULATION_KEY, []), candidate, _population_size()
        )
    best = pop.best(ctx.state.get(_POPULATION_KEY, []))
    if best is None:
        raise RuntimeError("population is empty; init must seed the baseline")
    remaining = _max_evaluations() - ctx.state.get(_EVALUATIONS_KEY, 0)
    if remaining <= 0:
        return Event(route=DONE, output={"best_id": best["id"]})
    return Event(route=CONTINUE, output=_brief(ctx.state))


def build_report(state: Any) -> dict[str, Any]:
    """Re-runs the baseline and the best rewrite; returns the numbers."""
    best = pop.best(state.get(_POPULATION_KEY, []))
    if best is None:
        raise RuntimeError("No verified candidates; run init first.")
    query = state[_QUERY_KEY]
    engine = get_engine()
    baseline = engine.run(query["sql"])
    if not baseline["ok"]:
        raise RuntimeError(f"Baseline no longer runs: {baseline['error']}")
    plan = verify_sql(best["sql"], baseline, query["ordered"])
    if not plan["valid"]:
        raise RuntimeError(f"Best rewrite no longer verifies: {plan['error']}")
    saved = baseline["bytes_scanned"] - plan["bytes_scanned"]
    history = state.get(_HISTORY_KEY, [])
    return {
        "query_title": query["title"],
        "question": query["question"],
        "engine": engine.name,
        "baseline_bytes_scanned": baseline["bytes_scanned"],
        "best_bytes_scanned": plan["bytes_scanned"],
        "bytes_saved": saved,
        "reduction_percent": round(100 * saved / baseline["bytes_scanned"], 1)
        if baseline["bytes_scanned"]
        else 0.0,
        "baseline_runtime_ms": baseline["runtime_ms"],
        "best_runtime_ms": plan["runtime_ms"],
        "result_rows": len(baseline["rows"]),
        "evaluations_used": state.get(_EVALUATIONS_KEY, 0),
        "rejected": sum(1 for h in history if not h["valid"]),
        "tried": [
            {k: h[k] for k in ("id", "idea", "bytes_scanned", "error")}
            for h in history
        ],
        "best_id": best["id"],
        "best_idea": best["idea"],
        "baseline_sql": query["sql"],
        "best_sql": best["sql"],
    }


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render_report(report: dict[str, Any]) -> str:
    """Formats the verifier's numbers as a Markdown report."""
    lines = [
        f"## SQL cost report: {report['query_title']}",
        "",
        f"_{report['question']}_ Engine: `{report['engine']}`. Every rewrite"
        f" below was run and returned the same {report['result_rows']} rows"
        " as the baseline.",
        "",
        "| | Bytes scanned | Runtime |",
        "| --- | --- | --- |",
        f"| Baseline query | {format_bytes(report['baseline_bytes_scanned'])} |"
        f" {report['baseline_runtime_ms']:.1f} ms |",
        f"| Best verified rewrite (candidate {report['best_id']}) |"
        f" **{format_bytes(report['best_bytes_scanned'])}** |"
        f" {report['best_runtime_ms']:.1f} ms |",
        f"| Reduction | **{format_bytes(report['bytes_saved'])}"
        f" ({report['reduction_percent']}%)** | |",
        "",
        "BigQuery on-demand pricing bills bytes processed, so the same"
        " reduction in bytes is the same reduction in that query's on-demand"
        " cost.",
        "",
        f"### Rewrites tried ({report['evaluations_used']} evaluations,"
        f" {report['rejected']} rejected by the verifier)",
        "",
        "| # | Idea | Bytes scanned |",
        "| --- | --- | --- |",
    ]
    for t in report["tried"]:
        cost = (
            format_bytes(t["bytes_scanned"])
            if t["bytes_scanned"] is not None
            else f"rejected: {_cell(t['error'])[:120]}"
        )
        lines.append(f"| {t['id']} | {_cell(t['idea'])} | {cost} |")
    lines += [
        "",
        "### Best rewrite",
        "",
        "```sql",
        report["best_sql"].rstrip(),
        "```",
        "",
        "<details><summary>Baseline query</summary>",
        "",
        "```sql",
        report["baseline_sql"].rstrip(),
        "```",
        "",
        "</details>",
    ]
    return "\n".join(lines)


def _for_narrator(numbers: dict[str, Any]) -> dict[str, Any]:
    """The report without SQL, with bytes formatted as they appear above."""
    out = {
        k: v
        for k, v in numbers.items()
        if k not in ("baseline_sql", "best_sql", "tried")
    }
    for key in ("baseline_bytes_scanned", "best_bytes_scanned", "bytes_saved"):
        out[key] = format_bytes(numbers[key])
    out["reduction_percent"] = f"{numbers['reduction_percent']}%"
    out["baseline_runtime_ms"] = f"{numbers['baseline_runtime_ms']:.1f} ms"
    out["best_runtime_ms"] = f"{numbers['best_runtime_ms']:.1f} ms"
    out["ideas_tried"] = [
        {
            "id": t["id"],
            "idea": t["idea"],
            "bytes": format_bytes(t["bytes_scanned"]),
        }
        for t in numbers["tried"]
    ]
    return out


def report(ctx: Context) -> Event:
    """Builds the cost report from verifier numbers only."""
    numbers = build_report(ctx.state)
    ctx.state[_REPORT_KEY] = render_report(numbers)
    return Event(
        message="All rewrites verified; writing the report.",
        output=_for_narrator(numbers),
    )


def deliver(ctx: Context, node_input: str) -> Event:
    """Sends one final answer: the verified report plus the summary.

    A2A clients such as the Gemini agent show the last message of a turn as
    the agent's answer, so the tables and the summary go out together.
    """
    summary = re.sub(r"\s+$", "", node_input or "")
    text = f"{ctx.state[_REPORT_KEY]}\n\n{summary}".strip()
    return Event(message=text, output=text)


# ---------------------------------------------------------------------------
# LLM nodes
# ---------------------------------------------------------------------------

PROPOSER_INSTRUCTION = """\
You are the proposer in an evolutionary loop that rewrites an analytics SQL
query to scan fewer bytes. A deterministic verifier runs every rewrite,
checks it returns exactly the baseline's result, and measures the bytes it
scans; you never score your own work.

The input JSON gives you the dialect, notes on how the engine reads data
(`engine_notes`), the table and its schema, the business question, the
baseline SQL with its bytes scanned and a preview of the expected result,
the best verified rewrites so far (`parents`, cheapest first, with SQL),
every rewrite tried so far with its verified result (`tried`), the verifier's
result for the latest rewrite (`last_feedback`), the evaluations remaining,
and an optional `operator_request` from the data engineer.

Return ONE new rewrite that builds on the strongest parent with a clear
hypothesis, for example: reference only the columns the result needs; drop
DISTINCT or joins that cannot change the result (use the schema notes);
replace functions on filtered columns with range predicates on the raw
column; collapse repeated scans of the table into one pass with conditional
aggregation or window functions; push filters into the scan. Keep exactly the
same output columns, column order, values, row count and ordering. If
`last_feedback` has an error, fix its cause. Do not repeat an entry from
`tried`. Write valid SQL for the given dialect, at most `max_sql_chars`
characters, as one read-only SELECT (WITH is fine), and use the table name
exactly as given. Do not claim a byte count or saving in `idea`; the
verifier measures it.
"""

NARRATOR_INSTRUCTION = """\
You write the closing summary of a SQL cost report for a data team. The
input JSON is the verified report: the query and question, the engine, bytes
scanned and runtime for the baseline and the best rewrite, the reduction,
and every idea tried.

Write at most 120 words of Markdown under the heading "### Summary": what
the best rewrite changed and why it scans less (in terms of columns read,
data skipped and number of scans), and how much it saves. Quote only numbers
that appear in the input, exactly as written but without quotation marks;
never compute, round or estimate a new number. End by noting that the
rewrite returned the same result in verification and should be reviewed
before it replaces the production query. Do not repeat the tables; they are
shown above your summary.
"""


def _model() -> Gemini:
    return Gemini(
        model=os.getenv("MODEL_NAME"),
        retry_options=types.HttpRetryOptions(attempts=3),
    )


def create_proposer() -> Agent:
    """The only creative step: writes one candidate rewrite per round."""
    return Agent(
        name="propose",
        model=_model(),
        mode="single_turn",
        include_contents="none",
        description="Writes one new candidate SQL rewrite.",
        instruction=PROPOSER_INSTRUCTION,
        input_schema=ProposalBrief,
        output_schema=Candidate,
    )


def create_narrator() -> Agent:
    """Summarizes the verified report without introducing new numbers."""
    return Agent(
        name="narrate",
        model=_model(),
        mode="single_turn",
        include_contents="none",
        description="Summarizes the verified cost report.",
        instruction=NARRATOR_INSTRUCTION,
    )


def create_workflow(
    proposer: BaseNode | None = None,
    narrator: BaseNode | None = None,
) -> Workflow:
    """Builds the propose -> verify -> select graph.

    Args:
        proposer: Node named "propose" that turns a ProposalBrief into a
            Candidate. Defaults to the Gemini proposer; tests pass a stub.
        narrator: Node named "narrate" that summarizes the report. Defaults
            to the Gemini narrator; tests pass a stub.
    """
    propose = proposer or create_proposer()
    narrate = narrator or create_narrator()
    return Workflow(
        name="evolutionary_llm_optimizer",
        description=(
            "Cuts the cost of an analytics SQL query: evolves rewrites with a"
            " propose -> verify -> select loop, proves each returns the same"
            " result, and reports the verified reduction in bytes scanned."
        ),
        edges=[
            ("START", init, {PROPOSE: propose, INVALID: reject}),
            (propose, verify, select),
            (select, {CONTINUE: propose, DONE: report}),
            (report, narrate, deliver),
        ],
    )


root_agent = create_workflow()

app = App(
    root_agent=root_agent,
    name="app",
)
