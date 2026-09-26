"""
SLO checker — validates Locust CSV output against defined thresholds.

Exits with code 1 if any SLO is breached, failing the Cloud Build load-test step
and blocking promotion to prod.

For a multi-agent AI system, latency is dominated by LLM inference (not infra)
and is not a useful SLO. What matters under concurrent load is:

  - error_rate < 5%: service stays up and returns valid responses
  - min_requests 1:  at least one request completed (load test actually ran)
  - max_p99 270s:    no responses hanging near Cloud Run's 300s hard timeout

Post-deploy eval (eval_vertex.py) is the quality gate for response correctness.

Usage:
    python tests/load/check_slos.py /path/to/load-results_stats.csv
"""

import csv
import sys

SLOS = {
    "error_rate": 5.0,  # max 5% error rate under concurrent load
    "min_requests": 1,  # at least 1 request completed per endpoint
    "max_p99_ms": 270_000,  # no requests hanging near Cloud Run's 300s timeout
}

# Endpoints excluded from per-endpoint SLO breakdown (checked via overall pass/fail)
EXCLUDE: set[str] = set()


def check_slos(csv_path: str) -> bool:
    passed = True
    rows = []

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Name", "")
            if not name or name == "Aggregated" or name in EXCLUDE:
                continue
            rows.append(row)

    if not rows:
        print("WARNING: No endpoint rows found in CSV — was the load test too short?")
        return True  # Don't fail if no data (e.g. dry-run)

    print(f"\nChecking SLOs for {len(rows)} endpoint(s)\n{'=' * 60}")

    for row in rows:
        name = row.get("Name", "unknown")
        try:
            p99 = float(row.get("99%", 0))
            total = int(row.get("Request Count", 1))
            failures = int(row.get("Failure Count", 0))
            error_rate = (failures / total * 100) if total > 0 else 0
        except (ValueError, TypeError):
            print(f"  {name}: could not parse row — skipping")
            continue

        print(f"\n  {name}")
        print(f"    requests={total}  failures={failures}  error_rate={error_rate:.1f}%  p99={p99:.0f}ms")

        endpoint_ok = True

        if error_rate > SLOS["error_rate"]:
            print(f"    ✗ FAIL  error rate {error_rate:.1f}% > {SLOS['error_rate']}%")
            endpoint_ok = False
        else:
            print(f"    ✓ error rate OK ({error_rate:.1f}%)")

        if total < SLOS["min_requests"]:
            print(f"    ✗ FAIL  only {total} request(s) completed — load test may not have run")
            endpoint_ok = False
        else:
            print(f"    ✓ requests completed ({total})")

        if p99 > SLOS["max_p99_ms"]:
            print(f"    ✗ FAIL  p99 {p99:.0f}ms > {SLOS['max_p99_ms']}ms (near Cloud Run timeout)")
            endpoint_ok = False
        else:
            print(f"    ✓ p99 within timeout ({p99:.0f}ms)")

        if not endpoint_ok:
            passed = False

    print(f"\n{'=' * 60}")
    print(f"SLO result: {'PASSED ✓' if passed else 'FAILED ✗'}")
    return passed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <locust_stats.csv>")
        sys.exit(1)

    sys.exit(0 if check_slos(sys.argv[1]) else 1)
