"""Test deployment of Guidelines-Driven Media Gen Agent with GCS file listing."""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import vertexai
from dotenv import load_dotenv
from google.adk.sessions import VertexAiSessionService
from vertexai import agent_engines
from google.cloud import storage

def pretty_print_event(event: dict) -> None:
    """Pretty prints an event with truncation for long content."""
    if "content" not in event:
        print(f"[{event.get('author', 'unknown')}]: {event}")
        return
        
    author = event.get("author", "unknown")
    parts = event["content"].get("parts", [])
    
    for part in parts:
        if "text" in part:
            text = part["text"]
            if len(text) > 200:
                text = text[:197] + "..."
            print(f"[{author}]: {text}")
        elif "functionCall" in part:
            func_call = part["functionCall"]
            print(f"[{author}]: Function call: {func_call.get('name', 'unknown')}")
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > 100:
                args = args[:97] + "..."
            print(f"  Args: {args}")
        elif "functionResponse" in part:
            func_response = part["functionResponse"]
            print(f"[{author}]: Function response: {func_response.get('name', 'unknown')}")
            response = json.dumps(func_response.get("response", {}))
            print(f"  Response: {response}")

CONFIG_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "deploy_config.json"))

def load_deploy_config() -> dict:
    """Loads configuration from deploy_config.json."""
    if not os.path.exists(CONFIG_FILE_PATH):
        return {}
    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def list_gcs_images(bucket_name: str) -> None:
    """Lists images created today in the GCS bucket."""
    print(f"\n--- Checking GCS Bucket: {bucket_name} ---")
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    current_date_str = datetime.utcnow().strftime("%Y-%m-%d")
    blobs = bucket.list_blobs(prefix=f"{current_date_str}/")
    
    images_found = []
    for blob in blobs:
        if blob.name.endswith(".png"):
            images_found.append(blob)
            
    if not images_found:
        print("No images found for today's date in GCS.")
        return
        
    print("\n[Generated Images in GCS]")
    # Sort by time created descending to show newest first
    images_found.sort(key=lambda x: x.time_created, reverse=True)
    for blob in images_found:
         gs_uri = f"gs://{bucket_name}/{blob.name}"
         http_link = f"https://storage.cloud.google.com/{bucket_name}/{blob.name}"
         print(f" - {gs_uri}")
         print(f"   View: {http_link}")
         print(f"   Created: {blob.time_created}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Test deployed Agent Engine agent.")
    parser.add_argument("--prompt", type=str, required=True, help="The prompt to send to the agent.")
    args = parser.parse_args()

    # Load .env relative to script location assuming it's in the project root
    env_file_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(env_file_path)

    config = load_deploy_config()

    project_id = config.get("project_id") or os.getenv("GOOGLE_CLOUD_PROJECT")
    location = config.get("location") or os.getenv("GOOGLE_CLOUD_LOCATION")
    bucket = config.get("bucket_name") or os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET") or os.getenv("GCS_BUCKET_NAME")
    resource_id = os.getenv("AGENT_ENGINE_ID") or config.get("resource_id")

    user_id = config.get("user_id") or "test-user"

    if not project_id:
        sys.exit("Missing required environment variable: GOOGLE_CLOUD_PROJECT")
    elif not location:
        sys.exit("Missing required environment variable: GOOGLE_CLOUD_LOCATION")
    elif not bucket:
        sys.exit("Missing AGENT_ENGINE_ID or GCS_BUCKET_NAME in configuration.")
    elif not resource_id:
        sys.exit("Missing AGENT_ENGINE_ID in .env and --resource_id was not provided.")

    vertexai.init(
        project=project_id,
        location=location,
        staging_bucket=f"gs://{bucket}" if not bucket.startswith("gs://") else bucket,
    )

    session_service = VertexAiSessionService(project_id, location)
    session = asyncio.run(session_service.create_session(
        app_name=resource_id,
        user_id=user_id)
    )

    agent = agent_engines.get(resource_id)
    print(f"Found agent with resource ID: {resource_id}")
    print(f"Created session for user ID: {user_id}")
    print(f"Prompt: {args.prompt}")

    try:
        print("\n--- Generating ---")
        for event in agent.stream_query(
            user_id=user_id,
            session_id=session.id,
            message=args.prompt
        ):
            pretty_print_event(event)

        # After finishing streaming, list the images in GCS
        list_gcs_images(bucket)

    finally:
        # Cleanup session on exit
        asyncio.run(session_service.delete_session(
            app_name=resource_id,
            user_id=user_id,
            session_id=session.id
        ))
        print(f"Deleted session for user ID: {user_id}")


if __name__ == "__main__":
    main()
