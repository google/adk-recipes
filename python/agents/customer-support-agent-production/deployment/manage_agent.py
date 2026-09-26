"""
Manage deployed Agent Engine instances.

This script provides utilities to list, query, and manage deployed agents.
"""

import argparse
import os
import sys


def list_agents(project_id: str, location: str):
    """List all deployed Agent Engine instances"""
    import vertexai

    vertexai.init(project=project_id, location=location)

    print(f"📋 Listing Agent Engine instances in {project_id} ({location})...")
    print()

    try:
        # List all reasoning engines (Agent Engine instances)
        from vertexai.preview import reasoning_engines

        engines = reasoning_engines.ReasoningEngine.list()

        if not engines:
            print("No Agent Engine instances found.")
            return

        for i, engine in enumerate(engines, 1):
            print(f"{i}. {engine.display_name}")
            print(f"   Resource Name: {engine.resource_name}")
            print(f"   Description: {engine.description or 'N/A'}")
            print(f"   Create Time: {engine.create_time}")
            print()

    except Exception as e:
        print(f"❌ Error listing agents: {str(e)}")
        sys.exit(1)


def query_agent(resource_name: str, message: str, user_id: str = "cli_user"):
    """Query a deployed Agent Engine instance"""
    import vertexai
    from vertexai import agent_engines

    # Extract project and location from resource name
    # Format: projects/{project}/locations/{location}/reasoningEngines/{id}
    parts = resource_name.split("/")
    project_id = parts[1]
    location = parts[3]

    vertexai.init(project=project_id, location=location)

    print(f"🤖 Querying agent: {resource_name}")
    print(f"💬 Message: {message}")
    print()

    try:
        # Get the deployed agent
        app = agent_engines.get(resource_name)

        # Create session
        session = app.create_session(user_id=user_id)
        print(f"📝 Session ID: {session.id}")

        # Query the agent
        print("🔄 Processing...")
        print()

        events = list(app.stream_query(user_id=user_id, session_id=session.id, message=message))

        print("=" * 70)
        print("📨 Response:")
        print()

        for event in events:
            if hasattr(event, "content"):
                print(event.content)

        print("=" * 70)

    except Exception as e:
        print(f"❌ Error querying agent: {str(e)}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


def delete_agent(resource_name: str):
    """Delete a deployed Agent Engine instance"""
    import vertexai
    from vertexai.preview import reasoning_engines

    # Extract project and location from resource name
    parts = resource_name.split("/")
    project_id = parts[1]
    location = parts[3]

    vertexai.init(project=project_id, location=location)

    print(f"🗑️  Deleting agent: {resource_name}")

    try:
        engine = reasoning_engines.ReasoningEngine(resource_name)
        engine.delete()
        print("✅ Agent deleted successfully")

    except Exception as e:
        print(f"❌ Error deleting agent: {str(e)}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Manage Agent Engine instances")
    parser.add_argument("command", choices=["list", "query", "delete"], help="Command to execute")
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT", "project-ddc15d84-7238-4571-a39"),
        help="Google Cloud project ID",
    )
    parser.add_argument(
        "--location", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"), help="Google Cloud location"
    )
    parser.add_argument(
        "--resource-name", default=os.getenv("AGENT_ENGINE_RESOURCE_NAME"), help="Agent Engine resource name"
    )
    parser.add_argument("--message", help="Message to send to the agent (for query command)")
    parser.add_argument("--user-id", default="cli_user", help="User ID for the session")

    args = parser.parse_args()

    if args.command == "list":
        list_agents(args.project, args.location)

    elif args.command == "query":
        if not args.resource_name:
            print("❌ Error: --resource-name is required for query command")
            print("Set AGENT_ENGINE_RESOURCE_NAME or use --resource-name flag")
            sys.exit(1)
        if not args.message:
            print("❌ Error: --message is required for query command")
            sys.exit(1)
        query_agent(args.resource_name, args.message, args.user_id)

    elif args.command == "delete":
        if not args.resource_name:
            print("❌ Error: --resource-name is required for delete command")
            print("Set AGENT_ENGINE_RESOURCE_NAME or use --resource-name flag")
            sys.exit(1)

        # Confirm deletion
        confirm = input(f"Are you sure you want to delete {args.resource_name}? (yes/no): ")
        if confirm.lower() == "yes":
            delete_agent(args.resource_name)
        else:
            print("Deletion cancelled")


if __name__ == "__main__":
    main()
