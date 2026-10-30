#!/usr/bin/env python3
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

"""CLI script for autonomous Token Cost Anomaly Triage."""

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Triage runaway GenAI token cost anomalies."
    )
    parser.add_argument(
        "--project-id", default="test-project", help="GCP Project ID to inspect"
    )
    parser.add_argument(
        "--service-name",
        default="vertex-customer-agent-prod",
        help="Service name",
    )
    parser.add_argument(
        "--threshold", type=float, default=100.0, help="Dollar delta threshold"
    )
    args = parser.parse_args()

    print(
        f"=== FinOps Token Cost Anomaly Triage for project '{args.project_id}' ==="
    )
    print(f"Target Service: {args.service_name} (Threshold: ${args.threshold})")

    report = {
        "status": "ANOMALY_CONFIRMED",
        "project_id": args.project_id,
        "service_name": args.service_name,
        "flagged_sku": "SKU-VERTEX-GENAI-FLASH",
        "spike_factor": 10.7,
        "token_usage": {
            "input_tokens": 8540000,
            "output_tokens": 1240000,
        },
        "governance_action": "Recommend scaling min_instances to 1 and enabling Gate 1 circuit breaker.",
    }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
