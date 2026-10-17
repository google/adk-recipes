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
"""An AlphaEvolve-style evolutionary code optimizer built with ADK.

Applied to a real platform-engineering decision: evolving a Kubernetes
workload placement program that cuts node cost while respecting every pod's
CPU and memory requests. The loop is: propose -> verify -> select.

- Propose: the Gemini agent writes a new candidate program, using the best
  programs found so far (and their scores) as parents.
- Verify: `evaluate_candidate` runs the candidate in a resource-limited
  subprocess and scores its outputs deterministically in this process.
- Select: the best verified candidates are kept in session state and fed back
  to the model for the next round, until the evaluation budget is spent or
  the known lower bound is reached.
"""

import os
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

from . import population as pop
from . import problem
from .sandbox import run_candidate

_POPULATION_KEY = "evolve_population"
_EVALUATIONS_KEY = "evolve_evaluations"
_NEXT_ID_KEY = "evolve_next_id"


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


def verify_program(code: str) -> dict[str, Any]:
    """Runs and scores a program against the benchmark. No model involved."""
    run = run_candidate(
        code,
        problem.ENTRY_POINT,
        problem.INSTANCES,
        timeout_seconds=_timeout_seconds(),
    )
    if not run["ok"]:
        return {"valid": False, "score": None, "error": run["error"]}
    return problem.score_outputs(run["outputs"])


def _register(
    tool_context: ToolContext, code: str, idea: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Assigns an id, updates the population in session state."""
    candidate_id = tool_context.state.get(_NEXT_ID_KEY, 0)
    tool_context.state[_NEXT_ID_KEY] = candidate_id + 1
    candidate = {
        "id": candidate_id,
        "idea": idea,
        "code": code,
        "valid": result["valid"],
        "score": result["score"],
    }
    tool_context.state[_POPULATION_KEY] = pop.add_candidate(
        tool_context.state.get(_POPULATION_KEY, []),
        candidate,
        _population_size(),
    )
    return candidate


def get_problem(tool_context: ToolContext) -> dict[str, Any]:
    """Returns the optimization problem and seeds the population with a baseline.

    Call this once at the start of an optimization run.

    Returns:
        The problem statement, the baseline program and its verified score,
        the lower bound on the score, and the evaluation budget.
    """
    baseline = verify_program(problem.BASELINE_PROGRAM)
    if not tool_context.state.get(_POPULATION_KEY):
        _register(
            tool_context,
            problem.BASELINE_PROGRAM,
            "baseline: single std-8 node pool, first-fit",
            baseline,
        )
        tool_context.state[_EVALUATIONS_KEY] = 0
    best = pop.best(tool_context.state[_POPULATION_KEY])
    return {
        "problem": problem.PROBLEM_STATEMENT,
        "baseline_program": problem.BASELINE_PROGRAM,
        "baseline_score": baseline["score"],
        "baseline_monthly_cost": baseline.get("monthly_cost"),
        "baseline_per_instance": baseline.get("per_instance"),
        "clusters": problem.describe_workloads(),
        "node_types": problem.NODE_TYPES,
        "best_score": best["score"] if best else None,
        "lower_bound": problem.lower_bound(),
        "evaluation_budget": _max_evaluations(),
        "evaluations_used": tool_context.state.get(_EVALUATIONS_KEY, 0),
    }


def get_parents(tool_context: ToolContext) -> dict[str, Any]:
    """Returns the best programs found so far, with code, to build on.

    Returns:
        The current population (best first), each with id, score, idea and code.
    """
    population = sorted(
        tool_context.state.get(_POPULATION_KEY, []),
        key=pop.rank_key,
    )
    return {
        "parents": [
            {
                "id": c["id"],
                "score": c["score"],
                "idea": c["idea"],
                "code": c["code"],
            }
            for c in population
        ],
        "lower_bound": problem.lower_bound(),
    }


def evaluate_candidate(
    code: str, idea: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Verifies and scores a candidate program, and updates the population.

    Args:
        code: Complete Python source defining `place(pods, node_types)`.
        idea: One sentence describing what changed versus the parent.

    Returns:
        The candidate's validity, score (lower is better) or error, whether it
        is a new best, the current best score, and the remaining budget.
    """
    used = tool_context.state.get(_EVALUATIONS_KEY, 0)
    if used >= _max_evaluations():
        return {
            "budget_exhausted": True,
            "message": "No evaluations left. Report results.",
        }
    tool_context.state[_EVALUATIONS_KEY] = used + 1

    previous_best = pop.best(tool_context.state.get(_POPULATION_KEY, []))
    result = verify_program(code)
    candidate = _register(tool_context, code, idea, result)
    current_best = pop.best(tool_context.state[_POPULATION_KEY])
    remaining = _max_evaluations() - (used + 1)
    reached_bound = (
        current_best is not None
        and current_best["score"] <= problem.lower_bound()
    )
    return {
        "candidate_id": candidate["id"],
        "valid": result["valid"],
        "score": result["score"],
        "monthly_cost": result.get("monthly_cost"),
        "per_instance": result.get("per_instance"),
        "error": result.get("error"),
        "is_new_best": bool(
            result["valid"]
            and (
                previous_best is None
                or result["score"] < previous_best["score"]
            )
        ),
        "best_score": current_best["score"] if current_best else None,
        "lower_bound": problem.lower_bound(),
        "evaluations_remaining": remaining,
        "done": remaining <= 0 or reached_bound,
        "population": pop.summarize(tool_context.state[_POPULATION_KEY]),
    }


def get_best_plan(tool_context: ToolContext) -> dict[str, Any]:
    """Returns the best verified placement plan and its savings vs the baseline.

    Call this once at the end to build the final report.

    Returns:
        Baseline and best hourly/monthly cost, savings, the node mix per
        cluster for the best program, and the best program's code.
    """
    best = pop.best(tool_context.state.get(_POPULATION_KEY, []))
    if best is None:
        return {"error": "No verified candidates yet. Call get_problem first."}
    baseline = verify_program(problem.BASELINE_PROGRAM)
    plan = verify_program(best["code"])
    if not plan["valid"]:
        return {"error": f"Best program no longer verifies: {plan['error']}"}
    saved = baseline["monthly_cost"] - plan["monthly_cost"]
    return {
        "baseline_monthly_cost": baseline["monthly_cost"],
        "best_monthly_cost": plan["monthly_cost"],
        "monthly_savings": round(saved, 2),
        "savings_percent": round(100 * saved / baseline["monthly_cost"], 1),
        "lower_bound_monthly_cost": round(
            problem.lower_bound() * problem.HOURS_PER_MONTH, 2
        ),
        "baseline_per_cluster": baseline["per_instance"],
        "best_per_cluster": plan["per_instance"],
        "best_idea": best["idea"],
        "best_code": best["code"],
    }


INSTRUCTION = """\
You are a Kubernetes cost-optimization agent that works like AlphaEvolve. You
evolve a workload placement program: which nodes to run and which pods go on
each, so every pod's CPU and memory requests fit and total node cost is as low
as possible. Every score comes from a deterministic verifier, never from you.

When the user asks you to start (or to optimize / evolve / cut cost / run):
1. Call `get_problem` once. Study each cluster's resource shape (memory per
   CPU core), the node menu and prices, the baseline cost and the lower bound.
2. Repeat until a tool result has `done: true` or `budget_exhausted: true`:
   a. Call `get_parents` and pick one or two strong parents.
   b. Propose ONE new complete program with a clear hypothesis, for example:
      choose node types whose memory/CPU ratio matches each cluster; sort
      pods by dominant resource share; best-fit on the tighter dimension;
      mix node types and right-size the last, partly empty nodes; local
      search that empties a node and re-places its pods. Keep it
      deterministic, standard-library only, and about a second per call.
   c. Call `evaluate_candidate` with the full code and a one-sentence `idea`.
   d. Use the cost, per-cluster node mix and any error to decide the next
      change. If a candidate is invalid, fix the cause; never resubmit
      identical code.
3. Call `get_best_plan` and finish with a report for a platform team:
   baseline vs best monthly cost and savings, the lower bound, the
   recommended node mix per cluster, a table of ideas tried with their
   verified costs, and the best program in a Python code block.

Costs are in USD. Lower is better. Do not ask for confirmation between
iterations.
"""


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the Agent."""
    return Agent(
        name="root_agent",
        model=Gemini(
            model=os.getenv("MODEL_NAME"),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description=(
            "Evolves a Kubernetes workload placement program that minimizes"
            " node cost, with a propose -> verify -> select loop."
        ),
        instruction=INSTRUCTION,
        tools=[get_problem, get_parents, evaluate_candidate, get_best_plan],
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
