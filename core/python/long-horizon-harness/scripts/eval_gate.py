"""Fail a graded eval run whose cases scored below the threshold.

`adk eval` enforced `threshold: 0.8` itself; `agents-cli eval grade` only reports
scores, so the pass/fail decision lives here.

    uv run python scripts/eval_gate.py [--results DIR] [--threshold 0.8]
"""

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass

DEFAULT_RESULTS_DIR = "artifacts/grade_results"
DEFAULT_THRESHOLD = 0.8


class GateError(Exception):
    pass


@dataclass(frozen=True)
class Failure:
    case_index: int
    metric: str
    score: float | None
    detail: str


def load_results(results_dir: str) -> dict:
    """Parse the newest `results_*.json` agents-cli wrote into results_dir."""
    files = sorted(glob.glob(os.path.join(results_dir, "results_*.json")))
    if not files:
        raise GateError(f"no results_*.json under {results_dir}")
    with open(files[-1], encoding="utf-8") as fh:
        return json.load(fh)


def failing_cases(results: dict, *, threshold: float) -> list[Failure]:
    failures: list[Failure] = []
    scored = 0
    for case in results.get("eval_case_results") or []:
        index = case.get("eval_case_index", -1)
        for candidate in case.get("response_candidate_results") or []:
            for metric, result in (
                candidate.get("metric_results") or {}
            ).items():
                error = result.get("error_message")
                if error:
                    failures.append(Failure(index, metric, None, error))
                    continue
                score = result.get("score")
                if score is None:
                    continue
                scored += 1
                if score < threshold:
                    failures.append(
                        Failure(
                            index, metric, score, f"{score:.2f} < {threshold}"
                        )
                    )
    if not scored and not failures:
        raise GateError(
            "no metric scores in the results file — grading did not run, or the "
            "result shape changed"
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)

    failures = failing_cases(
        load_results(args.results), threshold=args.threshold
    )
    for failure in failures:
        print(f"case[{failure.case_index}] {failure.metric}: {failure.detail}")
    if failures:
        print(f"\n{len(failures)} case(s) below {args.threshold}")
        return 1
    print(f"all cases at or above {args.threshold}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GateError as exc:
        print(f"eval gate: {exc}", file=sys.stderr)
        sys.exit(2)
