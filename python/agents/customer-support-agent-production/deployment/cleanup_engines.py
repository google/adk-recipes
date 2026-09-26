"""
Cleanup old Agent Engine (Reasoning Engine) resources.

Keeps the N most recently created versioned engines
(display name matching customer-support-multiagent-v*.*.*) and deletes the rest.

Usage:
    python deployment/cleanup_engines.py --project PROJECT_ID --region REGION --keep 2
"""

import argparse
import re
import sys

import vertexai
from google.cloud import aiplatform


def cleanup_old_engines(project: str, region: str, keep: int) -> None:
    vertexai.init(project=project, location=region)

    client = aiplatform.gapic.ReasoningEngineServiceClient(
        client_options={"api_endpoint": f"{region}-aiplatform.googleapis.com"}
    )
    parent = f"projects/{project}/locations/{region}"

    engines = list(client.list_reasoning_engines(parent=parent))

    versioned = [e for e in engines if re.match(r"customer-support-multiagent-v\d+\.\d+\.\d+$", e.display_name or "")]

    versioned.sort(key=lambda e: e.create_time, reverse=True)

    to_keep = versioned[:keep]
    to_delete = versioned[keep:]

    if not to_delete:
        print(f"Nothing to delete — found {len(versioned)} versioned engine(s), keeping all.")
        return

    print(f"Found {len(versioned)} versioned engine(s). Keeping {len(to_keep)}, deleting {len(to_delete)}.")
    for engine in to_keep:
        print(f"  KEEP:   {engine.display_name} ({engine.name})")
    for engine in to_delete:
        print(f"  DELETE: {engine.display_name} ({engine.name})")

    for engine in to_delete:
        try:
            op = client.delete_reasoning_engine(name=engine.name)
            op.result(timeout=300)
            print(f"Deleted: {engine.display_name}")
        except Exception as e:
            print(f"Warning: failed to delete {engine.display_name}: {e}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean up old Agent Engine resources")
    parser.add_argument("--project", required=True, help="GCP project ID")
    parser.add_argument("--region", default="us-central1", help="GCP region")
    parser.add_argument("--keep", type=int, default=2, help="Number of most recent engines to keep")
    args = parser.parse_args()

    cleanup_old_engines(project=args.project, region=args.region, keep=args.keep)
