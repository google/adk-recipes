"""
Deployment Script for Multi-Agent Customer Support System
==========================================================
Deploys to Vertex AI Agent Engine with Memory Bank.

Behaviour:
  - If an Agent Engine with the same display name already exists → UPDATE it
    (resource name stays the same, Cloud Run needs no change)
  - If none exists → CREATE it, then save the resource name to Secret Manager
    so Cloud Run and future CI runs can read it

Usage:
    python deployment/deploy.py --action [test_local|deploy|test_remote|cleanup]
"""

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import vertexai
from dotenv import load_dotenv
from google.adk.plugins.logging_plugin import LoggingPlugin
from vertexai import Client, agent_engines

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Load environment variables from .env file before importing customer_support_mas
load_dotenv()

from customer_support_mas.config import MODEL_ARMOR_CONFIG  # noqa: E402
from customer_support_mas.main import configure, root_agent  # noqa: E402

configure()

# =============================================================================
# CONFIGURATION
# =============================================================================

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
if not PROJECT_ID:
    raise ValueError("GOOGLE_CLOUD_PROJECT environment variable is required")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
STAGING_BUCKET = os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")
if not STAGING_BUCKET:
    raise ValueError("GOOGLE_CLOUD_STORAGE_BUCKET environment variable is required")
DISPLAY_NAME = os.getenv("AGENT_ENGINE_DISPLAY_NAME", "customer-support-multiagent")

REQUIREMENTS = [
    "google-cloud-aiplatform[adk,agent_engines]>=1.112",
    "google-cloud-firestore>=2.16.0",
    "google-cloud-modelarmor>=0.1.0",
    "requests",
    "numpy>=1.24.0",
    "vertexai>=1.38.0",
]

ENV_VARS = {
    "FIRESTORE_DATABASE": os.getenv("FIRESTORE_DATABASE", "customer-support-db"),
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
}

# Propagate Model Armor settings to Agent Engine runtime if enabled
if MODEL_ARMOR_CONFIG["enabled"] and MODEL_ARMOR_CONFIG["template_id"]:
    ENV_VARS["MODEL_ARMOR_ENABLED"] = "true"
    ENV_VARS["MODEL_ARMOR_TEMPLATE_ID"] = MODEL_ARMOR_CONFIG["template_id"]


# =============================================================================
# HELPERS
# =============================================================================


def build_plugins() -> list:
    """Build the ADK plugin list, conditionally including Model Armor."""
    plugins = [LoggingPlugin()]
    # NOTE: ModelArmorSafetyFilterPlugin is intentionally disabled for this deployment.
    # The backend (/api/chat) already screens user prompts via Model Armor before
    # they reach the agent, which is sufficient for this project.
    # Uncomment to enable ADK-level screening (useful when the agent is exposed
    # directly without a backend, or when tools fetch untrusted external content):
    #
    # if MODEL_ARMOR_CONFIG["enabled"] and MODEL_ARMOR_CONFIG["template_id"]:
    #     from customer_support_mas.safety import ModelArmorSafetyFilterPlugin
    #     plugins.append(
    #         ModelArmorSafetyFilterPlugin(template_id=MODEL_ARMOR_CONFIG["template_id"])
    #     )
    return plugins


def get_numeric_project_id(project_id: str) -> str:
    """Get the numeric project ID (required for Memory Bank model paths).

    In Cloud Build, PROJECT_NUMBER is injected as an env var — no gcloud needed.
    Falls back to gcloud for local runs.
    """
    # Cloud Build injects PROJECT_NUMBER as an env var
    project_number = os.getenv("PROJECT_NUMBER")
    if project_number:
        return project_number

    # Local fallback: use gcloud
    try:
        result = subprocess.run(
            ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"⚠️  Failed to get numeric project ID: {e}")
        return project_id


def init_vertex_ai():
    """Initialize Vertex AI SDK with project settings."""
    vertexai.init(
        project=PROJECT_ID,
        location=LOCATION,
        staging_bucket=STAGING_BUCKET,
    )
    print("✓ Initialized Vertex AI")
    print(f"  Project:  {PROJECT_ID}")
    print(f"  Location: {LOCATION}")
    print(f"  Staging:  {STAGING_BUCKET}")


def find_existing_agent_engine():
    """Find existing Agent Engine by display name. Returns the engine or None."""
    try:
        print(f"  Checking for existing Agent Engine '{DISPLAY_NAME}'...")
        for engine in agent_engines.list():
            if engine.display_name == DISPLAY_NAME:
                print(f"  ✓ Found existing: {engine.resource_name}")
                return engine
        print("  No existing Agent Engine found — will create new")
    except Exception as e:
        print(f"  ⚠️  Could not list Agent Engines: {e}")
    return None


def configure_memory_bank(client: Client, resource_name: str, numeric_project_id: str):
    """Configure Memory Bank on the Agent Engine resource."""
    print("\n⏳ Configuring Memory Bank...")
    client.agent_engines.update(
        name=resource_name,
        config={
            "context_spec": {
                "memory_bank_config": {
                    "generation_config": {
                        "model": f"projects/{numeric_project_id}/locations/{LOCATION}/publishers/google/models/gemini-2.5-flash"
                    },
                    "similarity_search_config": {
                        "embedding_model": f"projects/{numeric_project_id}/locations/{LOCATION}/publishers/google/models/gemini-embedding-001"
                    },
                }
            }
        },
    )
    print("✓ Memory Bank configured")


# =============================================================================
# LOCAL TESTING
# =============================================================================


async def test_locally():
    """Test the agent locally before deployment."""
    print("\n" + "=" * 60)
    print("LOCAL TESTING")
    print("=" * 60)

    app = agent_engines.AdkApp(
        agent=root_agent,
        app_name="customer_support",
        enable_tracing=True,
        plugins=build_plugins(),
    )

    session = await app.async_create_session(user_id="demo-user-001")
    print(f"\n✓ Created local session: {session.id}")

    test_queries = [
        "Hi, I need some help today",
        "Can you search for laptops?",
        "Where is my order ORD-12345?",
        "I need the invoice INV-2025-001",
    ]

    for i, query in enumerate(test_queries):
        if i > 0:
            print("  (waiting 15s to avoid rate limits...)")
            await asyncio.sleep(15)

        print(f"\n{'─' * 40}")
        print(f"USER: {query}")
        print(f"{'─' * 40}")

        try:
            async for event in app.async_stream_query(
                user_id="demo-user-001",
                session_id=session.id,
                message=query,
            ):
                content = event.get("content", {})
                for part in content.get("parts", []):
                    if part.get("text") and not part.get("function_call"):
                        print(f"\nAGENT: {part['text']}")
                    elif part.get("function_call"):
                        fn = part["function_call"]
                        print(f"\n  → Tool: {fn['name']}({fn.get('args', {})})")
        except Exception as e:
            print(f"\n⚠️  Query failed: {e}")
            print("   Continuing with next query...")

    print("\n✓ Local testing complete!")


# =============================================================================
# DEPLOYMENT — Update if exists, create if not
# =============================================================================


def deploy_to_agent_engine():
    """
    Deploy to Vertex AI Agent Engine with Memory Bank.

    - If an engine with display_name already exists → UPDATE (same resource name)
    - If none exists → CREATE + save resource name to Secret Manager
    - Always reconfigures Memory Bank after deploy/update
    """
    print("\n" + "=" * 70)
    print("DEPLOYING TO VERTEX AI AGENT ENGINE (with Memory Bank)")
    print("=" * 70)

    init_vertex_ai()

    numeric_project_id = get_numeric_project_id(PROJECT_ID)
    print(f"✓ Numeric Project ID: {numeric_project_id}")

    client = Client(project=numeric_project_id, location=LOCATION)

    adk_app = agent_engines.AdkApp(
        agent=root_agent,
        app_name="customer_support",
        enable_tracing=True,
        plugins=build_plugins(),
    )

    # -------------------------------------------------------------------------
    # Stage 1: Update existing or create new
    # -------------------------------------------------------------------------
    existing = find_existing_agent_engine()

    if existing:
        print("\n⏳ Stage 1/2: Updating existing Agent Engine...")
        existing.update(
            agent_engine=adk_app,
            requirements=REQUIREMENTS,
            extra_packages=["customer_support_mas"],
            env_vars=ENV_VARS,
        )
        resource_name = existing.resource_name
        print("✓ Agent Engine updated (resource name unchanged)")
    else:
        print("\n⏳ Stage 1/2: Creating new Agent Engine...")
        remote_app = agent_engines.create(
            agent_engine=adk_app,
            requirements=REQUIREMENTS,
            extra_packages=["customer_support_mas"],
            display_name=DISPLAY_NAME,
            env_vars=ENV_VARS,
        )
        resource_name = remote_app.resource_name
        print("✓ Agent Engine created!")

    agent_engine_id = resource_name.split("/")[-1]
    print(f"  Resource: {resource_name}")
    print(f"  ID:       {agent_engine_id}")

    # Write resource name to /workspace so the deploy-cloud-run step can read it.
    # This handles the first-create case where the resource name is not yet in .env.
    workspace_file = Path("/workspace/agent_engine_resource_name.txt")
    if workspace_file.parent.exists():
        workspace_file.write_text(resource_name)
        print(f"✓ Resource name written to {workspace_file}")

    # -------------------------------------------------------------------------
    # Stage 2: Configure Memory Bank
    # -------------------------------------------------------------------------
    configure_memory_bank(client, resource_name, numeric_project_id)

    print("\n" + "=" * 70)
    print("✅ DEPLOYMENT SUCCESSFUL!")
    print("=" * 70)
    print(f"\nResource Name:  {resource_name}")
    print(f"Agent Engine ID: {agent_engine_id}")
    if not existing:
        print("⚠️  Update AGENT_ENGINE_RESOURCE_NAME in .env with the resource name above.")
        print("    Cloud Build reads it as _AGENT_ENGINE_RESOURCE_NAME substitution.")
    print("\nView in Cloud Console:")
    print(f"  https://console.cloud.google.com/vertex-ai/agents/agent-engines?project={PROJECT_ID}")


# =============================================================================
# TEST DEPLOYED AGENT
# =============================================================================


async def test_remote_agent(resource_name: str):
    """Test the deployed agent on Agent Engine."""
    print("\n" + "=" * 60)
    print("TESTING DEPLOYED AGENT")
    print("=" * 60)

    init_vertex_ai()

    remote_app = agent_engines.get(resource_name)
    print(f"✓ Connected to: {resource_name}")

    session = await remote_app.async_create_session(user_id="remote_test_user")
    print(f"✓ Created session: {session['id']}")

    test_query = "Hi! Can you help me track order ORD-12345 and show me the invoice for it?"
    print(f"\n{'─' * 60}")
    print(f"USER: {test_query}")
    print(f"{'─' * 60}")

    async for event in remote_app.async_stream_query(
        user_id="remote_test_user",
        session_id=session["id"],
        message=test_query,
    ):
        content = event.get("content", {})
        for part in content.get("parts", []):
            if part.get("text") and not part.get("function_call"):
                print(f"\nAGENT: {part['text']}")

    print("\n✓ Remote testing complete!")


# =============================================================================
# CLEANUP
# =============================================================================


def cleanup_deployment(resource_name: str):
    """Delete the deployed agent to avoid charges."""
    print("\n" + "=" * 60)
    print("CLEANING UP DEPLOYMENT")
    print("=" * 60)

    init_vertex_ai()

    remote_app = agent_engines.get(resource_name)
    remote_app.delete(force=True)

    print(f"✓ Deleted: {resource_name}")
    print("✓ Cleanup complete!")


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Deploy Multi-Agent Customer Support to Vertex AI Agent Engine with Memory Bank"
    )
    parser.add_argument(
        "--action",
        choices=["test_local", "deploy", "test_remote", "cleanup"],
        required=True,
        help="Action to perform",
    )
    parser.add_argument(
        "--resource_name",
        type=str,
        help="Resource name for test_remote or cleanup actions",
    )

    args = parser.parse_args()

    if args.action == "test_local":
        asyncio.run(test_locally())

    elif args.action == "deploy":
        deploy_to_agent_engine()

    elif args.action == "test_remote":
        if not args.resource_name:
            print("ERROR: --resource_name required for test_remote")
            return
        asyncio.run(test_remote_agent(args.resource_name))

    elif args.action == "cleanup":
        if not args.resource_name:
            print("ERROR: --resource_name required for cleanup")
            return
        cleanup_deployment(args.resource_name)


if __name__ == "__main__":
    main()
