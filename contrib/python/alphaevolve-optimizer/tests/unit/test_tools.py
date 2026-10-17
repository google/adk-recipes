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
"""Unit tests for the verifier, selector and agent tools. No model calls."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import population as pop
from app import problem
from app.agent import (
    evaluate_candidate,
    get_best_plan,
    get_parents,
    get_problem,
    verify_program,
)
from app.sandbox import run_candidate

# Picks, per cluster, the single node type whose first-fit-decreasing packing
# is cheapest. A realistic "right-size the node pool" improvement.
RIGHT_SIZED_POOL = """\
def place(pods, node_types):
    best = None
    for t in node_types:
        order = sorted(range(len(pods)), key=lambda i: -max(
            pods[i]["cpu"] / t["cpu"], pods[i]["memory"] / t["memory"]))
        nodes, cpu, mem = [], [], []
        for i in order:
            for n in range(len(nodes)):
                if (cpu[n] + pods[i]["cpu"] <= t["cpu"]
                        and mem[n] + pods[i]["memory"] <= t["memory"]
                        and len(nodes[n]["pods"]) < t["max_pods"]):
                    nodes[n]["pods"].append(i)
                    cpu[n] += pods[i]["cpu"]
                    mem[n] += pods[i]["memory"]
                    break
            else:
                nodes.append({"node_type": t["name"], "pods": [i]})
                cpu.append(pods[i]["cpu"])
                mem.append(pods[i]["memory"])
        cost = len(nodes) * t["hourly_cost"]
        if best is None or cost < best[0]:
            best = (cost, nodes)
    return best[1]
"""

ONE_NODE_PER_POD = (
    "def place(pods, node_types):\n"
    "    return [{'node_type': 'std-16', 'pods': [i]} for i in range(len(pods))]\n"
)


@pytest.fixture
def tool_context() -> SimpleNamespace:
    return SimpleNamespace(state={})


@pytest.fixture(autouse=True)
def _small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_EVALUATIONS", "2")
    monkeypatch.setenv("POPULATION_SIZE", "3")


# --- problem / data ---------------------------------------------------------


def test_bundled_workloads_load_one_instance_per_cluster() -> None:
    names = [i["name"] for i in problem.INSTANCES]
    assert names == ["ecommerce-prod", "data-platform", "ml-serving"]
    assert all(i["args"]["pods"] for i in problem.INSTANCES)


def test_custom_workloads_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "w.csv"
    csv_path.write_text(
        "cluster,workload,replicas,cpu_millicores,memory_mib\n"
        "c1,api,3,500,1024\n"
        "c2,db,1,2000,8192\n"
    )
    instances = problem.load_instances(csv_path)
    assert [len(i["args"]["pods"]) for i in instances] == [3, 1]


# --- verifier ---------------------------------------------------------------


def test_baseline_is_valid_and_above_lower_bound() -> None:
    result = verify_program(problem.BASELINE_PROGRAM)
    assert result["valid"], result
    assert result["score"] >= problem.lower_bound()
    assert result["monthly_cost"] == pytest.approx(
        result["score"] * problem.HOURS_PER_MONTH, abs=0.01
    )


def test_right_sized_pool_is_cheaper_than_baseline() -> None:
    baseline = verify_program(problem.BASELINE_PROGRAM)
    improved = verify_program(RIGHT_SIZED_POOL)
    assert improved["valid"], improved
    assert improved["score"] < baseline["score"]


def test_wasteful_but_valid_plan_scores_worse() -> None:
    result = verify_program(ONE_NODE_PER_POD)
    assert result["valid"], result
    assert result["score"] > verify_program(problem.BASELINE_PROGRAM)["score"]


def test_overcommitted_node_is_rejected() -> None:
    code = (
        "def place(pods, node_types):\n"
        "    return [{'node_type': 'std-4', 'pods': list(range(len(pods)))}]\n"
    )
    result = verify_program(code)
    assert not result["valid"]
    assert "cpu" in result["error"] or "pods >" in result["error"]


def test_unknown_node_type_is_rejected() -> None:
    code = (
        "def place(pods, node_types):\n"
        "    return [{'node_type': 'gpu-xl', 'pods': [i]} for i in range(len(pods))]\n"
    )
    result = verify_program(code)
    assert not result["valid"]
    assert "node_type" in result["error"]


def test_missing_pods_are_rejected() -> None:
    result = verify_program("def place(pods, node_types):\n    return []\n")
    assert not result["valid"]
    assert "not placed" in result["error"]


def test_duplicate_pods_are_rejected() -> None:
    code = ONE_NODE_PER_POD.replace(
        "for i in range(len(pods))]", "for i in range(len(pods))] * 2"
    )
    result = verify_program(code)
    assert not result["valid"]
    assert "more than once" in result["error"]


def test_crashing_candidate_reports_error() -> None:
    result = verify_program(
        "def place(pods, node_types):\n    raise ValueError('boom')\n"
    )
    assert not result["valid"]
    assert "boom" in result["error"]


def test_infinite_loop_times_out() -> None:
    run = run_candidate(
        "def place(pods, node_types):\n    while True:\n        pass\n",
        problem.ENTRY_POINT,
        problem.INSTANCES,
        timeout_seconds=2,
    )
    assert not run["ok"]


def test_candidate_prints_do_not_corrupt_output() -> None:
    assert verify_program("print('noise')\n" + RIGHT_SIZED_POOL)["valid"]


def test_oversized_code_is_rejected() -> None:
    run = run_candidate("#" * 30_000, problem.ENTRY_POINT, problem.INSTANCES)
    assert not run["ok"]
    assert "exceeds" in run["error"]


# --- selector ---------------------------------------------------------------


def test_population_keeps_best_valid_candidates() -> None:
    population: list[dict] = []
    for cid, score in enumerate([5.0, 4.0, 6.0, 4.5]):
        population = pop.add_candidate(
            population, {"id": cid, "valid": True, "score": score}, max_size=2
        )
    assert [c["score"] for c in population] == [4.0, 4.5]


def test_population_ignores_invalid_candidates() -> None:
    population = pop.add_candidate(
        [], {"id": 0, "valid": False, "score": None}, max_size=2
    )
    assert population == []
    assert pop.best(population) is None


# --- agent tools ------------------------------------------------------------


def test_get_problem_seeds_baseline(tool_context: SimpleNamespace) -> None:
    info = get_problem(tool_context)
    assert (
        info["best_score"] == verify_program(problem.BASELINE_PROGRAM)["score"]
    )
    assert info["lower_bound"] == problem.lower_bound()
    assert len(info["clusters"]) == len(problem.INSTANCES)
    assert len(get_parents(tool_context)["parents"]) == 1


def test_evaluate_candidate_updates_population_and_budget(
    tool_context: SimpleNamespace,
) -> None:
    get_problem(tool_context)
    first = evaluate_candidate(
        RIGHT_SIZED_POOL, "right-size pool", tool_context
    )
    assert first["valid"]
    assert first["is_new_best"]
    assert first["evaluations_remaining"] == 1
    assert (
        get_parents(tool_context)["parents"][0]["score"] == first["best_score"]
    )

    invalid = evaluate_candidate(
        "def place(pods, node_types):\n    return []\n", "bad", tool_context
    )
    assert not invalid["valid"]
    assert invalid["done"]

    exhausted = evaluate_candidate(RIGHT_SIZED_POOL, "again", tool_context)
    assert exhausted["budget_exhausted"]


def test_get_best_plan_reports_savings(tool_context: SimpleNamespace) -> None:
    get_problem(tool_context)
    evaluate_candidate(RIGHT_SIZED_POOL, "right-size pool", tool_context)
    plan = get_best_plan(tool_context)
    assert plan["monthly_savings"] > 0
    assert plan["best_monthly_cost"] < plan["baseline_monthly_cost"]
    assert set(plan["best_per_cluster"]) == {
        i["name"] for i in problem.INSTANCES
    }
