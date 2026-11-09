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

"""Evaluation runner around `agents-cli eval` for System 1 (voice orchestrator)
and System 2 (coding harnesses on the user's computer) suites.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "tests" / "eval"
DATASETS_DIR = EVAL_DIR / "datasets"
sys.path.insert(0, str(EVAL_DIR))
from history import (  # noqa: E402
    HOLDOUT_SUFFIX,
    case_history_names,
    expectations_for,
)

CONFIG = EVAL_DIR / "eval_config_system1.yaml"
SYSTEM2_CONFIG = EVAL_DIR / "eval_config_system2.yaml"
FOCUS_DATASET = DATASETS_DIR / "system1.json"
SYSTEM2_DATASET = DATASETS_DIR / "system2.json"
SYSTEM2_FIXTURES = EVAL_DIR / "fixtures" / "alex"
SYSTEM2_IMAGE = "adk-voice-agent-computer:eval"
SYSTEM2_HARNESSES = "claude,antigravity"
SYSTEM1_CONCURRENCY = 6
RUBRICS_METRIC = "rubrics"


def run(argv: list[str], env: dict[str, str] | None = None) -> int:
    return subprocess.run(argv, cwd=ROOT, env=env, check=False).returncode  # noqa: S603 - fixed argv, no shell


def with_rubrics(case: dict) -> dict:
    if "reference" in case:
        return case
    rubrics = (
        case.get("rubrics")
        or (case.get("expectations") or {}).get("rubrics")
        or (expectations_for(case["eval_case_id"]) or {}).get("rubrics")
        or []
    )
    if not rubrics:
        return case
    text = "\n".join(f"{i}. {r}" for i, r in enumerate(rubrics, 1))
    return {
        **case,
        "reference": {"response": {"role": "model", "parts": [{"text": text}]}},
    }


def metrics(
    *,
    rubrics: bool,
    skip: set[str] | frozenset[str] = frozenset(),
    config: Path = CONFIG,
) -> list[str]:
    names = yaml.safe_load(config.read_text(encoding="utf-8"))["metrics_to_run"]
    return [
        m for m in names if m not in skip and (rubrics or m != RUBRICS_METRIC)
    ]


def dataset_history(dataset: Path) -> str:
    """`EVAL_HISTORY` when `--url` runs the whole dataset against one shared server."""
    cases = json.loads(dataset.read_text(encoding="utf-8")).get("eval_cases")
    histories = {
        tuple(case_history_names(case["eval_case_id"])) for case in cases or []
    }
    if len(histories) > 1:
        named = "; ".join(sorted(",".join(h) or "(none)" for h in histories))
        sys.exit(
            f"ERROR: {dataset.name}'s cases start from different histories ({named}), "
            "and one server serves the whole run. Run cases that share one with --cases "
            "(CASES=...), or set EVAL_HISTORY explicitly."
        )
    return ",".join(next(iter(histories), ()))


def eval_env() -> dict[str, str]:
    from dotenv import dotenv_values

    pinned = {
        k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None
    }
    return {
        **os.environ,
        **pinned,
        "PYTHONPATH": f"tests/eval{os.pathsep}.",
    }


def resolve_suite_defaults(
    suite: str,
    *,
    dataset: str | None = None,
    config: str | None = None,
) -> tuple[str, Path, Path, str]:
    norm = "system2" if suite.lower() in {"system2", "sandbox"} else "system1"
    if norm == "system2":
        ds = Path(dataset) if dataset else SYSTEM2_DATASET
        cfg = Path(config) if config else SYSTEM2_CONFIG
        return norm, ds, cfg, "adk"
    ds = Path(dataset) if dataset else FOCUS_DATASET
    cfg = Path(config) if config else CONFIG
    return norm, ds, cfg, "adk_live"


def build_eval_command(
    *,
    suite: str = "system1",
    dataset: str | Path | None = None,
    config: str | Path | None = None,
    output: str | Path | None = None,
    url: str | None = None,
    app_name: str | None = None,
    concurrency: int | None = None,
    qps: float | None = None,
    skip_metrics: str = "",
) -> list[str]:
    norm, ds_path, cfg_path, mode = resolve_suite_defaults(
        suite,
        dataset=str(dataset) if dataset else None,
        config=str(config) if config else None,
    )
    cmd = [
        "uv",
        "run",
        "agents-cli",
        "eval",
        "run",
        "--mode",
        mode,
        "--config",
        str(cfg_path),
        "--dataset",
        str(ds_path),
    ]
    if skip_metrics:
        skip = set(filter(None, skip_metrics.split(",")))
        active = metrics(rubrics=True, skip=skip, config=cfg_path)
        cmd += ["--metrics", ",".join(active)]
    if url:
        cmd += ["--url", url]
    if app_name or norm == "system2":
        cmd += ["--app-name", app_name or "system2_agent"]
    if concurrency is not None:
        cmd += ["--concurrency", str(concurrency)]
    if qps is not None:
        cmd += ["--qps", str(qps)]
    if output is not None:
        cmd += ["--output", str(output)]
    return cmd


def _select_cases(dataset: Path, case_filter: str) -> list[dict]:
    cases = (
        json.loads(dataset.read_text(encoding="utf-8")).get("eval_cases") or []
    )
    wanted = {c.strip() for c in case_filter.split(",") if c.strip()}
    if not wanted:
        return cases
    known = [c.get("eval_case_id", "") for c in cases]
    if unknown := sorted(wanted - set(known)):
        sys.exit(
            f"ERROR: no cases {unknown} in {dataset} (known: {', '.join(known)})."
        )
    return [c for c in cases if c.get("eval_case_id") in wanted]


@contextlib.contextmanager
def _filtered_dataset(dataset: Path, case_filter: str) -> Iterator[Path]:
    if not case_filter.strip(","):
        yield dataset
        return
    selected = [with_rubrics(c) for c in _select_cases(dataset, case_filter)]
    with tempfile.TemporaryDirectory(prefix="voice-agent-eval-") as tmp:
        filtered = Path(tmp) / dataset.name
        filtered.write_text(
            json.dumps({"eval_cases": selected}, indent=2) + "\n",
            encoding="utf-8",
        )
        yield filtered


class ServerExitedError(RuntimeError):
    pass


@contextlib.contextmanager
def _local_server(app: str, env: dict[str, str]) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log = open(Path(env["EVAL_RUN_DIR"]) / "server.log", "wb")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            app,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env={**env, "APP_URL": base},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and proc.poll() is None:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        if proc.poll() is not None:
            raise ServerExitedError(
                f"the eval server {app} exited; see {log.name}."
            )
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
        log.close()


@contextlib.contextmanager
def _system2_server(url: str | None, env: dict[str, str]) -> Iterator[str]:
    if url:
        yield url
        return
    try:
        with _local_server("system2_agent.server:app", env) as base:
            yield base
    except ServerExitedError as exc:
        sys.exit(f"ERROR: {exc}")


def system2_env(
    env: dict[str, str], *, harness: str, run_dir: Path, image: str
) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    out = {
        **env,
        "SYSTEM2_HARNESS": harness,
        "EVAL_RUN_DIR": str(run_dir),
        "THREAD_DB_URL": f"sqlite+aiosqlite:///{run_dir}/threads.db",
        "COMPUTER_PROVIDER": "docker",
        "COMPUTER_IMAGE": image,
        "VOICE_AGENT_WORKSPACES_LOCAL_YAML": str(
            SYSTEM2_FIXTURES / "workspaces.yaml"
        ),
        "VOICE_AGENT_SKIP_STARTUP_WARMUP": "1",
    }
    out.pop("GOOGLE_CLOUD_AGENT_ENGINE_ID", None)
    return out


def build_computer_image(tag: str) -> int:
    print(f"Building the computer image {tag} from sandbox/ ...", flush=True)
    return run(["docker", "build", "--tag", tag, str(ROOT / "sandbox")])


def system2_dataset(dataset: Path, out_dir: Path) -> Path:
    data = json.loads(dataset.read_text(encoding="utf-8"))
    cases = [with_rubrics(c) for c in data.get("eval_cases") or []]
    target = out_dir / dataset.name
    target.write_text(
        json.dumps({"eval_cases": cases}, indent=2) + "\n", encoding="utf-8"
    )
    return target


def run_system2(
    args: argparse.Namespace, dataset: Path, cfg_path: Path, out_dir: Path
) -> int:
    image = os.getenv("COMPUTER_IMAGE") or SYSTEM2_IMAGE
    if (
        not args.url
        and not args.no_build
        and (status := build_computer_image(image))
    ):
        return status
    env = eval_env()
    graded = system2_dataset(dataset, out_dir)
    status = 0
    for harness in [h.strip() for h in args.harness.split(",") if h.strip()]:
        run_dir = out_dir / harness
        henv = system2_env(env, harness=harness, run_dir=run_dir, image=image)
        with _system2_server(args.url, henv) as server_url:
            cmd = build_eval_command(
                suite="system2",
                dataset=graded,
                config=cfg_path,
                output=run_dir,
                url=server_url,
                app_name="system2_agent",
                concurrency=args.concurrency or 1,
                qps=args.qps,
                skip_metrics=args.skip_metrics,
            )
            status = run(cmd, henv) or status
    from system2_agent.report import write_report as write_system2_report

    print(write_system2_report(out_dir))
    return status


def case_dataset(case: dict, out_dir: Path) -> Path:
    target = out_dir / "cases" / f"{case['eval_case_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"eval_cases": [with_rubrics(case)]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def case_env(
    env: dict[str, str], case_id: str, out_dir: Path
) -> dict[str, str]:
    run_dir = out_dir / case_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return {
        **env,
        "EVAL_HISTORY": ",".join(case_history_names(case_id)),
        "EVAL_RUN_DIR": str(run_dir),
        "THREAD_DB_URL": f"sqlite+aiosqlite:///{run_dir}/threads.db",
        "VOICE_EVAL_FAKES": "1",
    }


def generate_command(dataset: Path, traces: Path, url: str) -> list[str]:
    return [
        "uv", "run", "agents-cli", "eval", "generate",
        "--mode", "adk_live", "--url", url, "--app-name", "app",
        "--dataset", str(dataset), "-o", str(traces),
    ]  # fmt: skip


def grade_command(
    traces: Path,
    config: Path,
    output: Path,
    *,
    qps: float | None = None,
    skip_metrics: str = "",
) -> list[str]:
    cmd = [
        "uv", "run", "agents-cli", "eval", "grade",
        "--traces", str(traces), "--config", str(config), "--output", str(output),
    ]  # fmt: skip
    if skip_metrics:
        skip = set(filter(None, skip_metrics.split(",")))
        cmd += [
            "--metrics",
            ",".join(metrics(rubrics=True, skip=skip, config=config)),
        ]
    if qps is not None:
        cmd += ["--qps", str(qps)]
    return cmd


def run_system1_case(case: dict, env: dict[str, str], out_dir: Path) -> int:
    case_id = case["eval_case_id"]
    dataset = case_dataset(case, out_dir)
    cenv = case_env(env, case_id, out_dir)
    if status := run(
        [sys.executable, str(EVAL_DIR / "seed_eval_store.py")], cenv
    ):
        return status
    traces = out_dir / "traces" / f"{case_id}.json"
    traces.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _local_server("app.fast_api_app:app", cenv) as base:
            return run(generate_command(dataset, traces, base), cenv)
    except ServerExitedError as exc:
        print(f"ERROR: {case_id}: {exc}", file=sys.stderr)
        return 1


def run_system1(
    args: argparse.Namespace, dataset: Path, cfg_path: Path, out_dir: Path
) -> int:
    from concurrent.futures import ThreadPoolExecutor

    cases = _select_cases(dataset, args.cases)
    env = eval_env()
    with ThreadPoolExecutor(args.concurrency or SYSTEM1_CONCURRENCY) as pool:
        statuses = list(
            pool.map(lambda case: run_system1_case(case, env, out_dir), cases)
        )
    grade_env = {**env, "EVAL_RUN_DIR": str(out_dir)}
    grade_env.pop("THREAD_DB_URL", None)
    cmd = grade_command(
        out_dir / "traces",
        cfg_path,
        out_dir,
        qps=args.qps,
        skip_metrics=args.skip_metrics,
    )
    status = run(cmd, grade_env)
    print(write_report(out_dir))
    return status or max(statuses, default=0)


def run_suite(args: argparse.Namespace) -> int:
    norm, ds_path, cfg_path, _ = resolve_suite_defaults(
        args.suite,
        dataset=args.dataset,
        config=args.config,
    )
    case_filter = args.cases
    out_dir = (
        Path(args.output)
        if getattr(args, "output", None)
        else (
            ROOT
            / "artifacts"
            / norm
            / f"{time.strftime('%Y%m%d-%H%M%S')}-{ds_path.stem}"
        )
    )

    per_case = norm == "system1" and not args.url
    if getattr(args, "dry_run", False):
        if per_case:
            for case in _select_cases(ds_path, case_filter):
                case_id = case["eval_case_id"]
                cmd = generate_command(
                    out_dir / "cases" / f"{case_id}.json",
                    out_dir / "traces" / f"{case_id}.json",
                    "<server>",
                )
                print(" ".join(cmd))
            cmd = grade_command(
                out_dir / "traces",
                cfg_path,
                out_dir,
                qps=args.qps,
                skip_metrics=args.skip_metrics,
            )
        else:
            cmd = build_eval_command(
                suite=norm,
                dataset=ds_path,
                config=cfg_path,
                output=out_dir,
                url=args.url,
                concurrency=args.concurrency,
                qps=args.qps,
                skip_metrics=args.skip_metrics,
            )
        print(" ".join(cmd))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    if per_case:
        return run_system1(args, ds_path, cfg_path, out_dir)

    env = eval_env()
    env["THREAD_DB_URL"] = f"sqlite+aiosqlite:///{out_dir}/threads.db"
    env["EVAL_RUN_DIR"] = str(out_dir)

    with _filtered_dataset(ds_path, case_filter) as active_dataset:
        if norm == "system2":
            return run_system2(args, active_dataset, cfg_path, out_dir)

        env["VOICE_EVAL_FAKES"] = "1"
        if "EVAL_HISTORY" not in env:
            env["EVAL_HISTORY"] = dataset_history(active_dataset)
        if status := run(
            [sys.executable, str(EVAL_DIR / "seed_eval_store.py")], env
        ):
            return status
        cmd = build_eval_command(
            suite=norm,
            dataset=active_dataset,
            config=cfg_path,
            output=out_dir,
            url=args.url,
            concurrency=args.concurrency,
            qps=args.qps,
            skip_metrics=args.skip_metrics,
        )
        return run(cmd, env)


def write_report(run_dir: Path) -> str:
    scores: dict[str, dict[str, list[float]]] = {}
    notes: dict[str, list[str]] = {}
    errors: dict[str, list[str]] = {}
    latest = {
        r.parent: r for r in sorted(run_dir.glob("*/trial-*/results_*.json"))
    }
    if not latest:
        latest = {r: r for r in sorted(run_dir.glob("**/results_*.json"))}
    for result in latest.values():
        fallback_case = (
            result.parent.parent.name
            if result.parent.name.startswith("trial-")
            else result.stem
        )
        data = json.loads(result.read_text(encoding="utf-8"))
        dataset = (data.get("evaluation_dataset") or [{}])[0] or {}
        case_ids = [
            c.get("eval_case_id") for c in dataset.get("eval_cases") or []
        ]
        for case_res in data.get("eval_case_results", []):
            index = case_res.get("eval_case_index")
            case = (
                case_res.get("eval_case_id")
                or (
                    case_ids[index]
                    if index is not None and index < len(case_ids)
                    else None
                )
                or fallback_case
            )
            for candidate in case_res.get("response_candidate_results") or []:
                for name, metric in (
                    candidate.get("metric_results") or {}
                ).items():
                    explanation = (
                        metric.get("explanation")
                        or metric.get("error_message")
                        or ""
                    )
                    if explanation == "no rubrics for this case":
                        continue
                    score = metric.get("score")
                    if score is None:
                        errors.setdefault(name, []).append(
                            f"{case}: {explanation}"[:200]
                        )
                        continue
                    scores.setdefault(case, {}).setdefault(name, []).append(
                        float(score)
                    )
                    if float(score) < 1.0:
                        notes.setdefault(case, []).append(
                            f"{name}: {explanation}"[:300]
                        )
    metric_names = sorted({m for per_case in scores.values() for m in per_case})
    summary: dict[str, dict[str, float]] = {}
    lines = [f"# {run_dir.name}", ""]
    for label, holdout in (("Dev", False), ("Held out", True)):
        cases = sorted(
            c for c in scores if c.endswith(HOLDOUT_SUFFIX) == holdout
        )
        if not cases:
            continue
        lines += [
            f"## {label}",
            "",
            "| case | " + " | ".join(metric_names) + " |",
            "|---|" + "---|" * len(metric_names),
        ]
        for case in cases:
            row = [_mean(scores[case].get(m)) for m in metric_names]
            lines.append(f"| {case} | " + " | ".join(row) + " |")
        overall = {
            m: sum(v for c in cases for v in scores[c].get(m, []))
            / max(1, sum(len(scores[c].get(m, [])) for c in cases))
            for m in metric_names
        }
        summary[label.lower().replace(" ", "_")] = overall
        lines += [
            "| **all** | "
            + " | ".join(f"**{overall[m]:.2f}**" for m in metric_names)
            + " |",
            "",
        ]
    if errors:
        lines += ["## Metrics that errored (not scored)", ""]
        lines += [
            f"- `{name}` x{len(e)}: {e[0]}"
            for name, e in sorted(errors.items())
        ]
        lines.append("")
    if notes:
        lines += ["## Below 1.0", ""]
        lines += [
            f"- `{case}` {note}"
            for case in sorted(notes)
            for note in notes[case]
        ]
    report_text = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(report_text, encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "overall": summary,
                "cases": {
                    c: {m: sum(v) / len(v) for m, v in per.items()}
                    for c, per in scores.items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report_text


def _mean(values: list[float] | None) -> str:
    return f"{sum(values) / len(values):.2f}" if values else "-"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("voice", "sandbox", "report", "system1", "system2"),
        help="Optional positional suite or 'report' command.",
    )
    parser.add_argument(
        "run",
        nargs="?",
        help="Run directory path when command is 'report'.",
    )
    parser.add_argument(
        "--suite",
        default="system1",
        choices=("system1", "system2", "voice", "sandbox"),
        help="Evaluation suite to run: 'system1' (ADK Voice Agent + handoff) or 'system2' (coding harnesses on the user's computer).",
    )
    parser.add_argument(
        "--cases",
        "--case",
        default="",
        help="Comma-separated eval_case_ids to run (default: all).",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Override dataset JSON path (default: system1.json for system1, "
        "system2.json for system2).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Override eval config YAML path.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Optional URL of an already-running ADK server.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output directory for traces and results.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="System 1: cases run at once, each on its own server "
        f"(default: {SYSTEM1_CONCURRENCY}); with --url or System 2, forwarded to "
        "`agents-cli eval run`.",
    )
    parser.add_argument(
        "--qps",
        type=float,
        default=None,
        help="Grading QPS forwarded to `agents-cli eval run`.",
    )
    parser.add_argument(
        "--harness",
        default=SYSTEM2_HARNESSES,
        help="System 2: comma-separated harnesses, each run over the whole dataset "
        f"(default: {SYSTEM2_HARNESSES}).",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help=f"System 2: use the existing computer image ($COMPUTER_IMAGE or {SYSTEM2_IMAGE}) "
        "instead of building sandbox/ first.",
    )
    parser.add_argument(
        "--skip-metrics",
        default="",
        help="Comma-separated metric names to skip.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the `agents-cli eval run` command without running it.",
    )
    args = parser.parse_args(argv)
    if args.command in {"voice", "sandbox", "system1", "system2"}:
        args.suite = args.command
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "report":
        if not args.run:
            sys.exit("Usage: run_eval.py report <run_dir>")
        if resolve_suite_defaults(args.suite)[0] == "system2":
            from system2_agent.report import (
                write_report as write_system2_report,
            )

            print(write_system2_report(Path(args.run)))
            return 0
        print(write_report(Path(args.run)))
        return 0
    return run_suite(args)


if __name__ == "__main__":
    sys.exit(main())
