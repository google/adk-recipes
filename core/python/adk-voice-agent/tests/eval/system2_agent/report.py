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

"""System 2 run report (`report.md` and `summary.json`)."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

from .graders import DIMENSIONS, NOT_APPLICABLE, checks_for

PASS_METRICS = (*DIMENSIONS, "rubrics")
RAW_METRICS = ("time_to_result_s", "cost_usd")
RUBRIC_BAR = 0.8
TIMINGS = (
    "computer_ready_s",
    "harness_start_s",
    "first_update_s",
    "approach_s",
    "execute_start_s",
    "result_s",
)


def summarize_usage(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {
        "runs": len(runs),
        "cost_usd": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "turns": 0,
    }
    names = {
        "input_tokens": (
            "input_tokens",
            "inputTokens",
            "prompt_tokens",
            "promptTokenCount",
        ),
        "output_tokens": (
            "output_tokens",
            "outputTokens",
            "completion_tokens",
            "candidatesTokenCount",
        ),
        "cache_tokens": (
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_read_tokens",
            "cachedContentTokenCount",
        ),
    }
    for run in runs:
        res = run.get("result") or {}
        if isinstance(res.get("result"), dict):
            # agy wraps its result: {"event": "result", "result": {..., "usage": {...}}}.
            res = res["result"]
        cost = res.get("total_cost_usd")
        if isinstance(cost, int | float):
            total["cost_usd"] = (total["cost_usd"] or 0.0) + float(cost)
        usage = res.get("usage") or res.get("stats") or {}
        if isinstance(usage, dict):
            for key, aliases in names.items():
                total[key] += sum(
                    int(usage[n])
                    for n in aliases
                    if isinstance(usage.get(n), int | float)
                )
        if isinstance(res.get("num_turns"), int):
            total["turns"] += res["num_turns"]
    context = total["input_tokens"] + total["cache_tokens"]
    total["context_per_turn"] = (
        round(context / total["turns"]) if total["turns"] else None
    )
    return total


def run_counts(report: dict[str, Any]) -> dict[str, int]:
    runs = len(report.get("usage_runs") or [])
    return {"runs": runs, "execute": runs}


def step_mode(step: dict[str, Any]) -> str:
    return "steer" if "steer" in step else "act"


def quick_threads(case: str, report: dict[str, Any]) -> set[str]:
    return {
        str(c.get("thread"))
        for c in checks_for({"eval_case_id": case}, report)
        if c.get("check") == "within"
    }


def told_user(report: dict[str, Any]) -> dict[str, int]:
    events = [
        e
        for thread in (report.get("threads") or {}).values()
        for e in thread.get("events") or []
    ]
    return {
        "updates": sum(
            1
            for e in events
            if e.get("kind") == "progress" and e.get("finding")
        ),
    }


def _case_ids(results: dict[str, Any]) -> list[str]:
    # Results carry no eval_case_id; extract the case ID from each prompt's spec.
    ids = []
    for dataset in results.get("evaluation_dataset") or []:
        for case in (dataset or {}).get("eval_cases") or []:
            parts = ((case.get("prompt") or {}).get("parts")) or [{}]
            text = parts[0].get("text") or ""
            try:
                ids.append(str(json.loads(text[text.find("{") :]).get("case")))
            except ValueError:
                ids.append(str(len(ids)))
    return ids


def _results(harness_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    files = sorted(harness_dir.glob("results_*.json"))
    if not files:
        return {}
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    ids = _case_ids(data)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for case in data.get("eval_case_results") or []:
        metrics: dict[str, dict[str, Any]] = {}
        for cand in case.get("response_candidate_results") or []:
            for name, res in (cand.get("metric_results") or {}).items():
                metrics[name] = {
                    "score": res.get("score"),
                    "explanation": res.get("explanation")
                    or res.get("error_message")
                    or "",
                }
        index = int(case.get("eval_case_index") or 0)
        out[ids[index] if index < len(ids) else str(index)] = metrics
    return out


def _case_reports(harness_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        p.parent.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(harness_dir.glob("cases/*/*/report.json"))
    }


def _passed(metric: str, res: dict[str, Any]) -> bool | None:
    if res.get("explanation") == NOT_APPLICABLE or res.get("score") is None:
        return None
    bar = RUBRIC_BAR if metric == "rubrics" else 1.0
    return float(res["score"]) >= bar


def _cell(metric: str, res: dict[str, Any] | None) -> str:
    if not res:
        return "-"
    passed = _passed(metric, res)
    if passed is None:
        return "·" if res.get("explanation") == NOT_APPLICABLE else "err"
    return f"{'✅' if passed else '❌'} {float(res['score']):.2f}"


def write_report(run_dir: Path) -> str:
    harnesses = sorted(
        d.name
        for d in run_dir.iterdir()
        if d.is_dir() and list(d.glob("results_*.json"))
    )
    results = {h: _results(run_dir / h) for h in harnesses}
    reports = {h: _case_reports(run_dir / h) for h in harnesses}
    cases = sorted({c for h in harnesses for c in results[h]})
    lines = [f"# System 2 run {run_dir.name}", ""]
    summary: dict[str, Any] = {"harnesses": {}, "cases": {}}

    lines += [
        "## Pass/fail per case and harness",
        "",
        "✅ pass, ❌ fail, · not applicable.",
        "",
    ]
    lines += [
        "| case | harness | "
        + " | ".join(PASS_METRICS)
        + " | time to result | cost |",
        "|---|---|" + "---|" * (len(PASS_METRICS) + 2),
    ]
    clusters: dict[str, dict[str, list[str]]] = {}
    for case in cases:
        for h in harnesses:
            metrics = results[h].get(case) or {}
            report = reports[h].get(case) or {}
            usage = (
                summarize_usage(report["usage_runs"])
                if "usage_runs" in report
                else report.get("usage") or {}
            )
            ttr = (metrics.get("time_to_result_s") or {}).get("score")
            cost = usage.get("cost_usd")
            lines.append(
                f"| {case} | {h} | "
                + " | ".join(_cell(m, metrics.get(m)) for m in PASS_METRICS)
                + f" | {f'{ttr:.0f}s' if ttr else '-'} | {f'${cost:.2f}' if cost is not None else '-'} |"
            )
            summary["cases"].setdefault(case, {})[h] = {
                m: (metrics.get(m) or {}).get("score")
                for m in (*PASS_METRICS, *RAW_METRICS)
            }
            for m in PASS_METRICS:
                res = metrics.get(m) or {}
                if _passed(m, res) is False:
                    for failure in str(res.get("explanation", "")).split("; "):
                        key = (
                            f"{m}: {failure.split(':', 1)[0]}"
                            if m != "rubrics"
                            else "rubrics (judge)"
                        )
                        clusters.setdefault(key, {}).setdefault(h, []).append(
                            case
                        )
    lines.append("")

    lines += ["## Failures", ""]
    for case in cases:
        for h in harnesses:
            for m in PASS_METRICS:
                res = (results[h].get(case) or {}).get(m) or {}
                if _passed(m, res) is False:
                    lines.append(
                        f"- `{case}` / {h} / **{m}**: {str(res.get('explanation'))[:500]}"
                    )
            if err := (reports[h].get(case) or {}).get("runner_error"):
                lines.append(f"- `{case}` / {h} / **runner**: {err[:300]}")
    lines.append("")

    lines += [
        "## Failure clusters",
        "",
        "| failing check | " + " | ".join(harnesses) + " |",
        "|---|" + "---|" * len(harnesses),
    ]
    for key in sorted(
        clusters, key=lambda k: -sum(len(v) for v in clusters[k].values())
    ):
        lines.append(
            f"| {key} | "
            + " | ".join(
                ", ".join(clusters[key].get(h, [])) or "-" for h in harnesses
            )
            + " |"
        )
    lines.append("")

    lines += [
        "## Latency (seconds from dispatch)",
        "",
        "Computer boot is timed apart: the runner starts the computer before dispatching, as warm hours would. "
        "`approach` is the first `update_user` (time to first useful update); mode is `act` (the default) or `steer`.",
        "",
    ]
    lines += [
        "| case | harness | thread | mode | boot | "
        + " | ".join(t.removesuffix("_s") for t in TIMINGS)
        + " |",
        "|---|---|---|---|---|" + "---|" * len(TIMINGS),
    ]
    fast: dict[str, dict[str, list[float]]] = {}
    for case in cases:
        for h in harnesses:
            report = reports[h].get(case) or {}
            boot = (report.get("computer") or {}).get("boot_s")
            modes = {s["as"]: step_mode(s) for s in report.get("steps") or []}
            quick = quick_threads(case, report)
            for alias, thread in (report.get("threads") or {}).items():
                timings = thread.get("timings") or {}
                mode = modes.get(alias, "")
                lines.append(
                    f"| {case} | {h} | {alias} | {mode} | {boot if boot is not None else '-'} | "
                    + " | ".join(
                        f"{timings[t]:.1f}"
                        if timings.get(t) is not None
                        else "-"
                        for t in TIMINGS
                    )
                    + " |"
                )
                if alias in quick:
                    for t in TIMINGS:
                        if timings.get(t) is not None:
                            fast.setdefault(h, {}).setdefault(t, []).append(
                                timings[t]
                            )
                    if boot is not None:
                        fast.setdefault(h, {}).setdefault("boot_s", []).append(
                            boot
                        )
    lines += [
        "",
        "### Quick requests, median over the threads with a latency bar",
        "",
        "| harness | boot | "
        + " | ".join(t.removesuffix("_s") for t in TIMINGS)
        + " |",
        "|---|---|" + "---|" * len(TIMINGS),
    ]
    for h in harnesses:
        med = {k: median(v) for k, v in (fast.get(h) or {}).items()}
        lines.append(
            f"| {h} | {med.get('boot_s', 0):.1f} | "
            + " | ".join(f"{med[t]:.1f}" if t in med else "-" for t in TIMINGS)
            + " |"
        )
    lines.append("")

    lines += [
        "## Runs and context",
        "",
        "Runs are harness processes. Context per "
        "turn is the prompt side (fresh + cached input tokens) over the model turns the harness "
        "reports; agy reports one turn per run, so its figure is the prompt tokens of a whole run.",
        "",
        "| case | harness | runs | updates | turns | context/turn | output tokens |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in cases:
        for h in harnesses:
            report = reports[h].get(case) or {}
            usage = summarize_usage(report.get("usage_runs") or [])
            counts = run_counts(report)
            ctx = usage.get("context_per_turn")
            told = told_user(report)
            lines.append(
                f"| {case} | {h} | {counts['runs']} | "
                f"{told['updates']} | "
                f"{usage['turns'] or '-'} | {f'{ctx:,}' if ctx else '-'} | {usage['output_tokens']:,} |"
            )
    lines.append("")

    lines += [
        "## Cost",
        "",
        "| harness | cases | cost reported | input tokens | output tokens | cache tokens |",
        "|---|---|---|---|---|---|",
    ]
    for h in harnesses:
        usages = [
            summarize_usage(r["usage_runs"])
            if "usage_runs" in r
            else (r.get("usage") or {})
            for r in reports[h].values()
        ]
        cost = sum(u.get("cost_usd") or 0.0 for u in usages)
        tokens = {
            k: sum(u.get(k) or 0 for u in usages)
            for k in ("input_tokens", "output_tokens", "cache_tokens")
        }
        lines.append(
            f"| {h} | {len(usages)} | ${cost:.2f} | {tokens['input_tokens']:,} | {tokens['output_tokens']:,} | {tokens['cache_tokens']:,} |"
        )
        passed = {
            m: sum(
                1
                for c in cases
                if _passed(m, (results[h].get(c) or {}).get(m) or {}) is True
            )
            for m in PASS_METRICS
        }
        applicable = {
            m: sum(
                1
                for c in cases
                if _passed(m, (results[h].get(c) or {}).get(m) or {})
                is not None
            )
            for m in PASS_METRICS
        }
        summary["harnesses"][h] = {
            "cost_usd": round(cost, 4),
            **tokens,
            "pass_rate": {
                m: f"{passed[m]}/{applicable[m]}" for m in PASS_METRICS
            },
        }
    lines.append("")
    text = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return text
