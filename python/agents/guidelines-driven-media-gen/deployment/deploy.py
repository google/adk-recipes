"""Deployment script for guidelines-driven-media-gen agent."""

import json
import logging
import os
import sys
from pathlib import Path

import vertexai
from dotenv import load_dotenv, set_key
from google.api_core import exceptions as google_exceptions
from google.cloud import storage
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp

from guidelines_driven_media_gen.agent import root_agent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Path to the .env file relative to this script
# Assuming script is in `deployment/` and .env is in the root
ENV_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))

AGENT_WHL_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "guidelines_driven_media_gen-0.1.0-py3-none-any.whl"))
CONFIG_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "deploy_config.json"))


def load_deploy_config() -> dict:
    """Loads configuration from deploy_config.json."""
    if not os.path.exists(CONFIG_FILE_PATH):
        logger.warning(f"Config file not found at: {CONFIG_FILE_PATH}. Using .env only.")
        return {}
    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {CONFIG_FILE_PATH}: {e}")
        return {}


def update_env_file(agent_engine_id: str, env_file_path: str) -> None:
    """Updates the .env file with the agent engine ID."""
    try:
        set_key(env_file_path, "AGENT_ENGINE_ID", agent_engine_id)
        logger.info(f"Updated AGENT_ENGINE_ID in {env_file_path} to {agent_engine_id}")
    except Exception as e:
        logger.error(f"Error updating .env file: {e}")


def setup_staging_bucket(project_id: str, location: str, bucket_name: str) -> str:
    """Checks if the staging bucket exists, creates it if not."""
    storage_client = storage.Client(project=project_id)
    try:
        bucket = storage_client.lookup_bucket(bucket_name)
        if bucket:
            logger.info(f"Staging bucket gs://{bucket_name} already exists.")
        else:
            logger.info(f"Staging bucket gs://{bucket_name} not found. Creating...")
            new_bucket = storage_client.create_bucket(
                bucket_name, project=project_id, location=location
            )
            logger.info(f"Successfully created staging bucket gs://{new_bucket.name} in {location}.")
            new_bucket.iam_configuration.uniform_bucket_level_access_enabled = True
            new_bucket.patch()
            logger.info(f"Enabled uniform bucket-level access for gs://{new_bucket.name}.")
    except google_exceptions.Forbidden as e:
        logger.error(f"Permission denied error for bucket gs://{bucket_name}. Ensure 'Storage Admin' role. Error: {e}")
        raise
    except google_exceptions.ClientError as e:
        logger.error(f"Failed to access bucket gs://{bucket_name}. Error: {e}")
        raise

    return f"gs://{bucket_name}"


def create(env_vars: dict[str, str], project_id: str, location: str, bucket_name: str, display_name: str = None) -> None:
    """Creates and deploys the agent."""
    staging_bucket_uri = setup_staging_bucket(project_id, location, bucket_name)
    
    vertexai.init(
        project=project_id,
        location=location,
        staging_bucket=staging_bucket_uri,
    )

    adk_app = AdkApp(
        agent=root_agent,
        enable_tracing=True,
    )


    logger.info(f"Using agent wheel file: {AGENT_WHL_FILE}")

    if not os.path.exists(AGENT_WHL_FILE):
        logger.error(f"Agent wheel file not found at: {AGENT_WHL_FILE}")
        print("\nPlease run 'uv build --wheel --out-dir deployment' from the root folder first.")
        raise FileNotFoundError(f"Agent wheel file not found: {AGENT_WHL_FILE}")

    # Change to deployment directory to ensure wheel is packaged at the root
    current_dir = os.getcwd()
    deployment_dir = os.path.dirname(AGENT_WHL_FILE)
    whl_filename = os.path.basename(AGENT_WHL_FILE)
    
    logger.info(f"Changing directory to {deployment_dir} to package requirements")
    os.chdir(deployment_dir)
    
    try:
        remote_agent = agent_engines.create(
            adk_app,
            requirements=[whl_filename],
            extra_packages=[whl_filename],
            env_vars=env_vars,
            display_name=display_name,
        )
    finally:
        # Restore working directory
        os.chdir(current_dir)

    
    logger.info(f"Created remote agent: {remote_agent.resource_name}")
    print(f"\nSuccessfully created agent: {remote_agent.resource_name}")
    
    update_env_file(remote_agent.resource_name, ENV_FILE_PATH)


def delete(resource_id: str, project_id: str, location: str) -> None:
    """Deletes the specified agent and removes it from .env"""
    vertexai.init(project=project_id, location=location)
    
    logger.info(f"Attempting to delete agent: {resource_id}")
    try:
        remote_agent = agent_engines.get(resource_id)
        remote_agent.delete(force=True)
        logger.info(f"Successfully deleted remote agent: {resource_id}")
        print(f"\nSuccessfully deleted agent: {resource_id}")
        
        # Remove from .env
        set_key(ENV_FILE_PATH, "AGENT_ENGINE_ID", "")
    except google_exceptions.NotFound:
        logger.error(f"Agent with resource ID {resource_id} not found.")
        print(f"\nAgent not found: {resource_id}")
    except Exception as e:
        logger.error(f"An error occurred while deleting agent {resource_id}: {e}")
        print(f"\nError deleting agent {resource_id}: {e}")


def main() -> None:
    config = load_deploy_config()
    load_dotenv(ENV_FILE_PATH)

    # Prioritize config file over environment variables
    project_id = config.get("project_id") or os.getenv("GOOGLE_CLOUD_PROJECT")
    location = config.get("location") or os.getenv("GOOGLE_CLOUD_LOCATION")
    bucket_name = config.get("bucket_name") or os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET") or (f"{project_id}-adk-staging" if project_id else None)
    resource_id = config.get("resource_id") or os.getenv("AGENT_ENGINE_ID")

    # Support positional argument override for quick operations
    operation = config.get("operation")
    if sys.argv[1:] and sys.argv[1] in ["create", "delete"]:
        operation = sys.argv[1]

    if not operation:
        sys.exit("Error: No operation specified! Set 'operation' in deploy_config.json to 'create' or 'delete', or pass as positional arg.")

    if project_id and project_id.startswith("REPLACE_"):
        sys.exit("Error: Please replace the placeholder values in deployment/deploy_config.json")

    if not project_id:
        sys.exit("Error: Missing required GCP Project ID. Set inside deploy_config.json or GOOGLE_CLOUD_PROJECT.")
    if not location:
        sys.exit("Error: Missing required GCP Location.")
    if not bucket_name and operation == "create":
        sys.exit("Error: Missing GCS Bucket Name for creation.")
    if operation == "delete" and not resource_id:
        sys.exit("Error: No resource ID provided for deletion.")

    env_var_keys = [
        "GCS_BUCKET_NAME",
        "SCORE_THRESHOLD",
        "MAX_ITERATIONS",
        "IMAGE_GEN_MODEL",
        "GENAI_MODEL",
    ]

    env_vars = {}
    for key in env_var_keys:
        value = config.get(key) or os.getenv(key)
        if value and value.strip():
            env_vars[key] = value

    logger.info(f"Environment variables/parameters explicitly passed: {list(env_vars.keys())}")
    logger.info(f"Using PROJECT: {project_id}, LOCATION: {location}")

    try:
        if operation == "create":
            display_name = config.get("display_name")
            create(env_vars, project_id, location, bucket_name, display_name=display_name)
        elif operation == "delete":
            delete(resource_id, project_id, location)
    except Exception as e:
        logger.exception("Unhandled exception in main:")
        print(f"\nAn unexpected error occurred: {e}")


if __name__ == "__main__":
    # Change working directory to project root if run from deployment/
    if os.path.basename(os.getcwd()) == "deployment":
        os.chdir("..")
    main()
