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

import argparse
import datetime
import json
import logging
import os
import sys
from typing import Any

import tomllib
import vertexai
from dotenv import load_dotenv, set_key
from google.cloud import storage
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp

# Add the project root to sys.path
# Since we are now in root/presentation_agent/deployment/deploy.py,
# the project root is two levels up.
project_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

ENV_FILE_PATH = os.path.abspath(os.path.join(project_root, ".env"))
# Force override from .env, loaded BEFORE importing agent to ensure correct config
load_dotenv(ENV_FILE_PATH, override=True)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

if project_root not in sys.path:
    sys.path.insert(0, project_root)
from presentation_agent.agent import root_agent  # noqa: E402
from presentation_agent.shared_libraries.config import (  # noqa: E402
    resolve_regional_location,
)


# Function to update the .env file
def update_env_file(agent_engine_id, env_file_path):
    """Updates the .env file with the agent engine ID."""
    try:
        set_key(env_file_path, "AGENT_ENGINE_ID", agent_engine_id)
        logger.info(
            f"Updated AGENT_ENGINE_ID in {env_file_path} to {agent_engine_id}"
        )
    except Exception as e:
        logger.error(f"Error updating .env file: {e}")


def write_deployment_metadata(
    remote_agent: Any,
    metadata_file: str = "deployment_metadata.json",
) -> None:
    """Write deployment metadata to file."""
    metadata = {
        "remote_agent_engine_id": remote_agent.resource_name,
        "deployment_target": "agent_engine",
        "is_a2a": False,
        "deployment_timestamp": datetime.datetime.now().isoformat(),
    }

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    logger.info(f"Agent Engine ID written to {metadata_file}")


def load_requirements():
    """Loads requirements from pyproject.toml."""
    pyproject_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "pyproject.toml")
    )
    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)
    return pyproject_data["project"]["dependencies"]


def setup_staging_bucket(
    project_id: str, location: str, bucket_name: str
) -> str:
    """Checks if the staging bucket exists, creates it if not."""
    storage_client = storage.Client(project=project_id)
    bucket_name_without_prefix = bucket_name.replace("gs://", "")

    try:
        bucket = storage_client.lookup_bucket(bucket_name_without_prefix)
        if bucket:
            logger.info(
                "Staging bucket gs://%s already exists.",
                bucket_name_without_prefix,
            )
        else:
            logger.info(
                "Staging bucket gs://%s not found. Creating...",
                bucket_name_without_prefix,
            )
            new_bucket = storage_client.create_bucket(
                bucket_name_without_prefix,
                project=project_id,
                location=location,
            )
            logger.info(
                "Successfully created staging bucket gs://%s in %s.",
                new_bucket.name,
                location,
            )
            new_bucket.iam_configuration.uniform_bucket_level_access_enabled = (
                True
            )
            new_bucket.patch()
            logger.info(
                "Enabled uniform bucket-level access for gs://%s.",
                new_bucket.name,
            )
    except Exception as e:
        logger.error(
            f"Failed to create or access bucket gs://{bucket_name_without_prefix}. Error: {e}"
        )
        raise

    return f"gs://{bucket_name_without_prefix}"


def handle_default_template(project_id, bucket_name):
    """Checks for DEFAULT_TEMPLATE_URI, uploads the default template if not set."""
    if os.getenv("DEFAULT_TEMPLATE_URI"):
        logger.info("DEFAULT_TEMPLATE_URI is already set.")
        return os.getenv("DEFAULT_TEMPLATE_URI")

    logger.info(
        "DEFAULT_TEMPLATE_URI not set, handling default template upload..."
    )
    storage_client = storage.Client(project=project_id)
    bucket_name_without_prefix = bucket_name.replace("gs://", "")
    bucket = storage_client.bucket(bucket_name_without_prefix)

    source_file_name = "docs/Proposal_Template.pptx"
    destination_blob_name = "Proposal_Template.pptx"
    blob = bucket.blob(destination_blob_name)

    if not blob.exists():
        logger.info(
            f"Uploading {source_file_name} to gs://{bucket_name_without_prefix}/{destination_blob_name}..."
        )
        blob.upload_from_filename(source_file_name)
        logger.info("Upload complete.")
    else:
        logger.info(f"{destination_blob_name} already exists in the bucket.")

    return f"gs://{bucket_name_without_prefix}/{destination_blob_name}"


def main(mode):
    GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
    GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
    # Agent Engine and GCS staging bucket require a regional location (e.g. us-central1).
    # Multi-region endpoint locations ("global" or "us") used by Gemini 3.5+
    # fall back to a regional location for deployment resources.
    deployment_location = resolve_regional_location(GOOGLE_CLOUD_LOCATION)
    # Ensure GCP_STAGING_BUCKET is just the name, setup_staging_bucket will add gs:// prefix
    GCP_STAGING_BUCKET_NAME = (os.getenv("GCP_STAGING_BUCKET") or "").replace(
        "gs://", ""
    )
    if not GCP_STAGING_BUCKET_NAME:
        logger.warning(
            f"GCP_STAGING_BUCKET is not set or is empty. Defaulting to '{GOOGLE_CLOUD_PROJECT}-staging-bucket'."
        )
        GCP_STAGING_BUCKET_NAME = f"{GOOGLE_CLOUD_PROJECT}-staging-bucket"

    AGENT_ENGINE_ID = os.getenv("AGENT_ENGINE_ID")

    # Set up staging bucket
    GCP_STAGING_BUCKET = setup_staging_bucket(
        GOOGLE_CLOUD_PROJECT, deployment_location, GCP_STAGING_BUCKET_NAME
    )

    vertexai.init(
        project=GOOGLE_CLOUD_PROJECT,
        location=deployment_location,
        staging_bucket=GCP_STAGING_BUCKET,
    )

    default_template_uri = handle_default_template(
        GOOGLE_CLOUD_PROJECT, GCP_STAGING_BUCKET
    )

    # Build env_vars dynamically, ensuring all values are strings
    env_vars = {
        "GCP_PROJECT": str(GOOGLE_CLOUD_PROJECT or ""),
        "GCP_LOCATION": str(deployment_location),
        "GOOGLE_CLOUD_PROJECT": str(GOOGLE_CLOUD_PROJECT or ""),
        "GOOGLE_CLOUD_LOCATION": str(GOOGLE_CLOUD_LOCATION or "global"),
        "GCP_STAGING_BUCKET": str(GCP_STAGING_BUCKET or ""),
        "DEFAULT_TEMPLATE_URI": default_template_uri,
        "ENABLE_RAG": str(os.getenv("ENABLE_RAG") or "false"),
        "ENABLE_DEEP_RESEARCH": str(
            os.getenv("ENABLE_DEEP_RESEARCH") or "false"
        ),
        "GOOGLE_GENAI_USE_VERTEXAI": str(
            os.getenv("GOOGLE_GENAI_USE_VERTEXAI") or "True"
        ),
    }

    # Only set model overrides if explicitly defined in the local environment
    if os.getenv("GEMINI_MODEL_NAME"):
        env_vars["GEMINI_MODEL_NAME"] = str(os.getenv("GEMINI_MODEL_NAME"))
    if os.getenv("IMAGE_GENERATION_MODEL"):
        env_vars["IMAGE_GENERATION_MODEL"] = str(
            os.getenv("IMAGE_GENERATION_MODEL")
        )

    # Only add optional variables if they actually contain a value
    if os.getenv("DATASTORE_ID"):
        env_vars["DATASTORE_ID"] = str(os.getenv("DATASTORE_ID"))
    if os.getenv("MODEL_ARMOR_TEMPLATE_ID"):
        env_vars["MODEL_ARMOR_TEMPLATE_ID"] = str(
            os.getenv("MODEL_ARMOR_TEMPLATE_ID")
        )

    if mode == "update":
        logger.info("updating app in agent engine...")
        if AGENT_ENGINE_ID is not None:
            app = AdkApp(
                agent=root_agent,
                enable_tracing=True,
            )
            remote_agent = agent_engines.get(AGENT_ENGINE_ID)
            remote_agent.update(
                agent_engine=app,
                display_name="presentation_agent",
                requirements=load_requirements(),
                extra_packages=[
                    "./presentation_agent",
                ],
                env_vars=env_vars,
            )
            write_deployment_metadata(remote_agent)
        else:
            logger.info("no exisiting agent engine app resource found in env")

    elif mode == "create":
        logger.info("creating app in agent engine...")

        app = AdkApp(
            agent=root_agent,
            enable_tracing=True,
        )

        remote_agent = agent_engines.create(
            app,
            display_name="presentation_agent",
            requirements=load_requirements(),
            extra_packages=[
                "./presentation_agent",
            ],
            env_vars=env_vars,
        )

        logger.info(
            f"Deployed agent to Vertex AI Agent Engine successfully, resource name: {remote_agent.resource_name}"
        )
        update_env_file(remote_agent.resource_name, ENV_FILE_PATH)
        write_deployment_metadata(remote_agent)
    else:
        logger.info("invalid mode")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy or Update")
    parser.add_argument(
        "--mode",
        type=str,
        default="create",
        help="Select if the deplying for the first time or updating existing",
    )
    args = parser.parse_args()
    main(args.mode)
