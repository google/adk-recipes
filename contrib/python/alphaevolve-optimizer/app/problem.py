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
"""The optimization problem: cost-optimal Kubernetes workload placement.

Platform teams pay for nodes, not pods. Given every pod's CPU and memory
*requests* and a menu of node types with allocatable capacity and an hourly
price, the agent evolves a placement program that decides which nodes to run
and which pods go on each, minimizing total hourly cost. This is the core
decision behind node-pool sizing, cluster autoscaler tuning and capacity
planning.

This module is the only problem-specific part of the recipe. To evolve a
different program, replace the statement, instances, baseline and
`score_outputs`, keeping the same names.

Workloads are read from `WORKLOADS_CSV` (defaults to the bundled sample in
`app/data/workloads.csv`) with columns:
`cluster, workload, replicas, cpu_millicores, memory_mib`.
"""

import csv
import os
from pathlib import Path
from typing import Any

ENTRY_POINT = "place"

HOURS_PER_MONTH = 730

# Illustrative node menu. Allocatable capacity is what remains after system
# reservations; prices are illustrative on-demand USD/hour, roughly in line
# with general-purpose cloud VM ratios. Replace them with your own node types
# and negotiated prices.
NODE_TYPES: list[dict[str, Any]] = [
    {
        "name": "std-4",
        "cpu": 3920,
        "memory": 13312,
        "max_pods": 110,
        "hourly_cost": 0.134,
    },
    {
        "name": "std-8",
        "cpu": 7910,
        "memory": 28160,
        "max_pods": 110,
        "hourly_cost": 0.268,
    },
    {
        "name": "std-16",
        "cpu": 15890,
        "memory": 58368,
        "max_pods": 110,
        "hourly_cost": 0.536,
    },
    {
        "name": "highmem-4",
        "cpu": 3920,
        "memory": 27648,
        "max_pods": 110,
        "hourly_cost": 0.181,
    },
    {
        "name": "highmem-8",
        "cpu": 7910,
        "memory": 57344,
        "max_pods": 110,
        "hourly_cost": 0.362,
    },
    {
        "name": "highcpu-8",
        "cpu": 7910,
        "memory": 5632,
        "max_pods": 110,
        "hourly_cost": 0.198,
    },
    {
        "name": "highcpu-16",
        "cpu": 15890,
        "memory": 12288,
        "max_pods": 110,
        "hourly_cost": 0.396,
    },
]

PROBLEM_STATEMENT = """\
Write a Python function
`place(pods: list[dict], node_types: list[dict]) -> list[dict]`
that decides which Kubernetes nodes to run and which pods go on each node.

- Each pod is {"workload": str, "cpu": int millicores, "memory": int MiB}.
- Each node type is {"name": str, "cpu": int, "memory": int, "max_pods": int,
  "hourly_cost": float}; cpu and memory are ALLOCATABLE capacity.
- Return a list of nodes: {"node_type": <name>, "pods": [pod INDICES]}.
- Every pod index must appear on exactly one node. On every node the summed
  cpu and memory must fit the node type, and the pod count must be
  <= max_pods. You may use any mix of node types, any number of each.
- Use only the Python standard library. Be deterministic (seed any
  randomness). Each call must finish in about a second.

Objective: minimize the TOTAL hourly cost of all nodes over all clusters.
"""

BASELINE_PROGRAM = '''\
def place(pods, node_types):
    """A typical default: one std-8 node pool, first-fit in arrival order."""
    node = next(t for t in node_types if t["name"] == "std-8")
    nodes, cpu_used, mem_used = [], [], []
    for i, pod in enumerate(pods):
        for n in range(len(nodes)):
            if (cpu_used[n] + pod["cpu"] <= node["cpu"]
                    and mem_used[n] + pod["memory"] <= node["memory"]
                    and len(nodes[n]["pods"]) < node["max_pods"]):
                nodes[n]["pods"].append(i)
                cpu_used[n] += pod["cpu"]
                mem_used[n] += pod["memory"]
                break
        else:
            nodes.append({"node_type": node["name"], "pods": [i]})
            cpu_used.append(pod["cpu"])
            mem_used.append(pod["memory"])
    return nodes
'''

_DEFAULT_CSV = Path(__file__).parent / "data" / "workloads.csv"


def load_instances(
    path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Loads clusters from a workloads CSV; one benchmark instance per cluster."""
    path = path or os.getenv("WORKLOADS_CSV") or _DEFAULT_CSV
    clusters: dict[str, list[dict[str, Any]]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pod = {
                "workload": row["workload"],
                "cpu": int(row["cpu_millicores"]),
                "memory": int(row["memory_mib"]),
            }
            clusters.setdefault(row["cluster"], []).extend(
                dict(pod) for _ in range(int(row["replicas"]))
            )
    return [
        {"name": name, "args": {"pods": pods, "node_types": NODE_TYPES}}
        for name, pods in clusters.items()
    ]


INSTANCES: list[dict[str, Any]] = load_instances()


def _cluster_lower_bound(pods: list[dict[str, Any]]) -> float:
    """Fractional bound: no node buys CPU or memory cheaper than the best rate."""
    cpu_rate = min(t["hourly_cost"] / t["cpu"] for t in NODE_TYPES)
    mem_rate = min(t["hourly_cost"] / t["memory"] for t in NODE_TYPES)
    total_cpu = sum(p["cpu"] for p in pods)
    total_mem = sum(p["memory"] for p in pods)
    return max(total_cpu * cpu_rate, total_mem * mem_rate)


def lower_bound() -> float:
    """A cost no valid placement can beat (sum of per-cluster bounds)."""
    return round(
        sum(_cluster_lower_bound(i["args"]["pods"]) for i in INSTANCES), 4
    )


def _validate_placement(nodes: Any, pods: list[dict[str, Any]]) -> str | None:
    """Returns an error message, or None if `nodes` is a valid placement."""
    types = {t["name"]: t for t in NODE_TYPES}
    if not isinstance(nodes, list):
        return f"expected a list of nodes, got {type(nodes).__name__}"
    seen: set[int] = set()
    for n, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("node_type") not in types:
            return f"node {n} has no valid node_type"
        node_type = types[node["node_type"]]
        members = node.get("pods")
        if not isinstance(members, list):
            return f"node {n} pods must be a list"
        if len(members) > node_type["max_pods"]:
            return f"node {n} has {len(members)} pods > max_pods"
        cpu = mem = 0
        for idx in members:
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or not 0 <= idx < len(pods)
            ):
                return f"node {n} has invalid pod index {idx!r}"
            if idx in seen:
                return f"pod {idx} is placed more than once"
            seen.add(idx)
            cpu += pods[idx]["cpu"]
            mem += pods[idx]["memory"]
        if cpu > node_type["cpu"]:
            return f"node {n} ({node_type['name']}) cpu {cpu}m > {node_type['cpu']}m"
        if mem > node_type["memory"]:
            return f"node {n} ({node_type['name']}) memory {mem}Mi > {node_type['memory']}Mi"
    if len(seen) != len(pods):
        missing = sorted(set(range(len(pods))) - seen)[:5]
        return (
            f"{len(pods) - len(seen)} pods not placed (e.g. indices {missing})"
        )
    return None


def score_outputs(outputs: list[Any]) -> dict[str, Any]:
    """Scores candidate outputs, one placement per cluster.

    Returns:
        `valid`, `score` (total USD/hour, lower is better, None if invalid),
        `per_instance` cost and node mix per cluster, and `error`.
    """
    if len(outputs) != len(INSTANCES):
        return {
            "valid": False,
            "score": None,
            "error": f"expected {len(INSTANCES)} outputs, got {len(outputs)}",
        }
    prices = {t["name"]: t["hourly_cost"] for t in NODE_TYPES}
    per_instance: dict[str, Any] = {}
    for inst, nodes in zip(INSTANCES, outputs, strict=True):
        error = _validate_placement(nodes, inst["args"]["pods"])
        if error:
            return {
                "valid": False,
                "score": None,
                "error": f"cluster {inst['name']}: {error}",
            }
        mix: dict[str, int] = {}
        for node in nodes:
            mix[node["node_type"]] = mix.get(node["node_type"], 0) + 1
        per_instance[inst["name"]] = {
            "hourly_cost": round(sum(prices[t] * c for t, c in mix.items()), 4),
            "nodes": dict(sorted(mix.items())),
        }
    score = round(sum(c["hourly_cost"] for c in per_instance.values()), 4)
    return {
        "valid": True,
        "score": score,
        "monthly_cost": round(score * HOURS_PER_MONTH, 2),
        "per_instance": per_instance,
        "error": None,
    }


def describe_workloads() -> list[dict[str, Any]]:
    """Per-cluster totals, so the model can reason about resource shape."""
    out = []
    for inst in INSTANCES:
        pods = inst["args"]["pods"]
        cpu = sum(p["cpu"] for p in pods)
        mem = sum(p["memory"] for p in pods)
        out.append(
            {
                "cluster": inst["name"],
                "pods": len(pods),
                "total_cpu_millicores": cpu,
                "total_memory_mib": mem,
                "memory_mib_per_cpu_core": round(mem / (cpu / 1000), 1),
                "lower_bound_hourly_cost": round(_cluster_lower_bound(pods), 4),
            }
        )
    return out
