#!/usr/bin/env python3
# Copyright 2025 Google LLC
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

"""Deploy Ad Campaign Agent to Agent Engine using Python SDK (Inline Deployment).

This script uses "inline deployment" to set location='global',
which is required for Gemini 3.x models (gemini-3.7-flash, etc.).

The Python SDK allows vertexai.init(location='global') which bypasses
the CLI limitation where --region=global throws errors.

Usage:
    python scripts/deploy_ae_inline.py                  # Deploy with global region
    python scripts/deploy_ae_inline.py --trace          # Deploy with Cloud Trace
    python scripts/deploy_ae_inline.py --dry-run        # Show config without deploying
    python scripts/deploy_ae_inline.py --region=us-central1  # Override region if needed

Environment Variables:
    GOOGLE_CLOUD_PROJECT  - GCP project ID (default: kaggle-on-gcp)
    GCS_BUCKET            - GCS bucket name (default: kaggle-on-gcp-ad-campaign-assets)

See: https://google.github.io/adk-docs/deploy/agent-engine/
"""

import argparse
import os
import subprocess
import sys

# Check Python version - Agent Engine only supports 3.9-3.13
SUPPORTED_VERSIONS = ("3.9", "3.10", "3.11", "3.12", "3.13")
current_version = f"{sys.version_info.major}.{sys.version_info.minor}"
if current_version not in SUPPORTED_VERSIONS:
    print(
        f"\033[0;31m✗ Python {current_version} is not supported by Agent Engine.\033[0m"
    )
    print(f"  Supported versions: {', '.join(SUPPORTED_VERSIONS)}")
    print("\n  To fix this, run with a compatible Python version:")
    print("    python3.12 scripts/deploy_ae_inline.py")
    print("    # or")
    print("    pyenv local 3.12")
    print("    python scripts/deploy_ae_inline.py")
    sys.exit(1)

# Add project root to path for app imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_DIR)

# Colors for terminal output
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
CYAN = "\033[0;36m"
NC = "\033[0m"  # No Color


def print_header(text: str):
    """Print a styled header."""
    print(f"\n{BLUE}{'=' * 60}{NC}")
    print(f"{BLUE}{text}{NC}")
    print(f"{BLUE}{'=' * 60}{NC}")


def print_success(text: str):
    """Print success message."""
    print(f"{GREEN}✓ {text}{NC}")


def print_info(text: str):
    """Print info message."""
    print(f"{CYAN}  {text}{NC}")


def print_warning(text: str):
    """Print warning message."""
    print(f"{YELLOW}⚠ {text}{NC}")


def print_error(text: str):
    """Print error message."""
    print(f"{RED}✗ {text}{NC}")


def get_project_number(project_id: str) -> str:
    """Get the project number from project ID."""
    try:
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "gcloud",
                "projects",
                "describe",
                project_id,
                "--format=value(projectNumber)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print_warning(f"Could not get project number: {e}")
        return None


def grant_gcs_permissions(project_id: str, bucket_name: str) -> bool:
    """Grant GCS permissions to the Reasoning Engine Service Agent.

    Agent Engine uses a dedicated service account that needs storage.objectAdmin
    to write generated videos and thumbnails to GCS.

    Service account format: service-{PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com

    Args:
        project_id: GCP project ID
        bucket_name: GCS bucket name (without gs:// prefix)

    Returns:
        True if permissions were granted successfully
    """
    print_header("Granting GCS Permissions")

    project_number = get_project_number(project_id)
    if not project_number:
        print_warning(
            "Skipping GCS permission setup (could not get project number)"
        )
        print_warning(
            "You may need to manually grant storage.objectAdmin to the Reasoning Engine service account"
        )
        return False

    service_account = (
        f"service-{project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
    )
    print_info(f"Service Account: {service_account}")
    print_info(f"Bucket: gs://{bucket_name}")

    # Grant storage.objectAdmin at project level for simplicity
    # This allows the agent to write to any bucket in the project
    try:
        print(
            "\nGranting storage.objectAdmin to Reasoning Engine service account..."
        )
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "gcloud",
                "projects",
                "add-iam-policy-binding",
                project_id,
                f"--member=serviceAccount:{service_account}",
                "--role=roles/storage.objectAdmin",
                "--condition=None",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        print_success(f"Granted storage.objectAdmin to {service_account}")
        return True
    except subprocess.CalledProcessError as e:
        # Check if it's just a "binding already exists" situation
        if "already exists" in e.stderr.lower() or e.returncode == 0:
            print_success("Permission already granted (no change needed)")
            return True
        print_warning(f"Could not grant GCS permissions: {e.stderr}")
        print_warning("You may need to manually run:")
        print_warning(
            f"  gcloud projects add-iam-policy-binding {project_id} \\"
        )
        print_warning(f"    --member=serviceAccount:{service_account} \\")
        print_warning("    --role=roles/storage.objectAdmin")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Ad Campaign Agent to Agent Engine using Python SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s                          # Deploy to us-central1 (default)
    %(prog)s --trace                  # Deploy with Cloud Trace enabled
    %(prog)s --dry-run                # Preview configuration
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show configuration without deploying",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Enable Cloud Trace for observability",
    )
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT", "kaggle-on-gcp"),
        help="GCP project ID (default: GOOGLE_CLOUD_PROJECT env or kaggle-on-gcp)",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("GCS_BUCKET", "kaggle-on-gcp-ad-campaign-assets"),
        help="GCS bucket name (default: GCS_BUCKET env or kaggle-on-gcp-ad-campaign-assets)",
    )
    parser.add_argument(
        "--region",
        default="us-central1",
        help="Deployment region (default: us-central1, the only supported Agent Engine region)",
    )
    parser.add_argument(
        "--display-name",
        default="Ad Campaign Agent",
        help="Display name in Agent Engine console",
    )
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Skip granting GCS permissions to Reasoning Engine service account",
    )
    args = parser.parse_args()

    # Import here to avoid slow startup for --help
    import vertexai
    from vertexai import agent_engines

    print_header("Agent Engine Deployment (Python SDK)")

    print()
    print("Configuration:")
    print_info(f"Project:        {args.project}")
    print_info(f"Region:         {args.region}")
    print_info(f"Staging Bucket: gs://{args.bucket}")
    print_info(f"Display Name:   {args.display_name}")
    print_info(f"Cloud Trace:    {args.trace}")
    print_info("Method:         Python SDK (inline deployment)")
    print()

    if args.region != "us-central1":
        print_warning(
            "Warning: Agent Engine currently only supports us-central1"
        )
        print_warning(f"Using {args.region} may fail")
    else:
        print_success("Using us-central1 (Agent Engine supported region)")

    # Initialize Vertex AI
    print("\nInitializing Vertex AI...")
    vertexai.init(
        project=args.project,
        location=args.region,
        staging_bucket=f"gs://{args.bucket}",
    )
    print_success("Vertex AI initialized")

    if args.dry_run:
        print(
            f"\n{YELLOW}[DRY RUN]{NC} Configuration validated. Would deploy with settings above."
        )
        print("\nTo deploy for real, run without --dry-run:")
        print(f"  python {sys.argv[0]}")
        return 0

    # Import root_agent from app
    print("\nImporting agent from app.agent...")
    try:
        from app.agent import root_agent

        print_success(f"Agent imported: {root_agent.name}")
    except ImportError as e:
        print(f"{RED}✗ Failed to import agent: {e}{NC}")
        print("Make sure you're running from the project root directory.")
        return 1

    # Wrap in GlobalAdkApp (custom class that preserves GOOGLE_CLOUD_LOCATION)
    # This allows Gemini 3 models to use the global endpoint even when
    # Agent Engine is deployed to us-central1
    print(
        "\nCreating GlobalAdkApp wrapper (preserves global location for Gemini 3)..."
    )
    try:
        from app.agent_engine_app import GlobalAdkApp

        app = GlobalAdkApp(
            agent=root_agent,
            enable_tracing=args.trace,
        )
        print_success(
            "GlobalAdkApp created (will restore GOOGLE_CLOUD_LOCATION=global after set_up)"
        )
    except ImportError:
        print_warning("GlobalAdkApp not found, falling back to standard AdkApp")
        app = agent_engines.AdkApp(
            agent=root_agent,
            enable_tracing=args.trace,
        )
        print_success("AdkApp created")

    # Deploy
    print_header("Deploying to Agent Engine")
    print()
    print("This typically takes 5-10 minutes...")
    print("(Container is built and deployed automatically)")
    print()

    try:
        requirements = [
            "google-cloud-aiplatform[adk,agent_engines]>=1.79.0",
            "google-adk>=1.21.0",
            "google-cloud-storage>=2.19.0",
            "google-genai>=2.14.0",
            "googlemaps>=4.10.0",
            "Pillow>=10.2.0",
            "pydantic>=2.11.7",
            "numpy>=1.26.0",
            "matplotlib>=3.8.0",
        ]

        # Environment variables for the deployed container
        # GEMINI_MODEL_LOCATION is used by GlobalAdkApp to restore GOOGLE_CLOUD_LOCATION
        # after Agent Engine's set_up() override
        env_vars = {
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",  # Use Vertex AI (not AI Studio)
            "GEMINI_MODEL_LOCATION": "global",  # For Gemini 3 models
            "GCS_BUCKET": args.bucket,
            "APP_MODE": os.environ.get(
                "APP_MODE", "demo"
            ),  # Phase 6: inert until Phase 11/12
        }

        # Use standard module-level agent_engines.create()
        # extra_packages bundles the local app/ directory so remote has access to app.tools, etc.
        remote_app = agent_engines.create(
            agent_engine=app,
            requirements=requirements,
            extra_packages=[
                "./app"
            ],  # Bundle local app package for remote access
            env_vars=env_vars,  # Pass env vars including GEMINI_MODEL_LOCATION
            display_name=args.display_name,
            description="Fashion retail ad campaign management with Gemini Omni Flash video generation",
        )
    except Exception as e:
        print(f"\n{RED}✗ Deployment failed: {e}{NC}")
        return 1

    print_header("DEPLOYMENT COMPLETE!")

    print()
    print_success(f"Resource Name: {remote_app.resource_name}")

    # Grant GCS permissions to the Reasoning Engine service account
    # This is required for the agent to write generated videos to GCS
    if not args.skip_permissions:
        grant_gcs_permissions(args.project, args.bucket)
    else:
        print_warning("Skipping GCS permission setup (--skip-permissions flag)")
        print_warning(
            "Make sure the Reasoning Engine service account has storage.objectAdmin role"
        )
    print()
    print(f"{GREEN}Query API Endpoint:{NC}")
    print(
        f"  https://{args.region}-aiplatform.googleapis.com/v1/{remote_app.resource_name}:query"
    )
    print()
    print(f"{GREEN}Stream Query API Endpoint:{NC}")
    print(
        f"  https://{args.region}-aiplatform.googleapis.com/v1/{remote_app.resource_name}:streamQuery"
    )
    print()

    # Extract resource ID for convenience
    resource_id = remote_app.resource_name.split("/")[-1]

    print(f"{GREEN}Python Usage:{NC}")
    print(f"""
    from vertexai import agent_engines
    import vertexai

    vertexai.init(project="{args.project}", location="{args.region}")
    agent = agent_engines.get("{remote_app.resource_name}")
    response = agent.query(input="List all campaigns")
    print(response)
    """)

    print(f"{GREEN}Useful Commands:{NC}")
    print("  # List deployed agents")
    print(
        f"  gcloud ai reasoning-engines list --project={args.project} --region={args.region}"
    )
    print()
    print("  # Delete this agent")
    print(
        f"  gcloud ai reasoning-engines delete {resource_id} --project={args.project} --region={args.region}"
    )
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
