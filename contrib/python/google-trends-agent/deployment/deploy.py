# Copyright 2025 Google LLC
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

import logging
import os
import sys

import tomllib
import vertexai
from dotenv import load_dotenv, set_key
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp

from app.agent import root_agent

# Add the project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
STAGING_BUCKET = os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")

# Agent Engine is hosted in a real region (NOT the model-serving "global"
# endpoint). The agent's model calls still go to global — that is forced in
# app/__init__.py — but the reasoning engine itself must live in a region
# such as us-central1. Set AGENT_ENGINE_LOCATION in .env (see .env.example).
AGENT_ENGINE_LOCATION = os.getenv("AGENT_ENGINE_LOCATION")

ENV_FILE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".env")
)

vertexai.init(
    project=GOOGLE_CLOUD_PROJECT,
    location=AGENT_ENGINE_LOCATION,
    staging_bucket=STAGING_BUCKET,
)


# Function to update the .env file
def update_env_file(agent_engine_id, env_file_path):
    """Updates the .env file with the agent engine ID."""
    try:
        set_key(env_file_path, "AGENT_ENGINE_ID", agent_engine_id)
        print(
            f"Updated AGENT_ENGINE_ID in {env_file_path} to {agent_engine_id}"
        )
    except Exception as e:
        print(f"Error updating .env file: {e}")


def load_requirements():
    """Loads requirements from pyproject.toml."""
    pyproject_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
    )
    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)
    return pyproject_data["project"]["dependencies"]


logger.info("deploying app...")

app = AdkApp(
    agent=root_agent,
    enable_tracing=True,
)

logging.debug("deploying agent to agent engine:")

remote_app = agent_engines.create(
    app,
    display_name="google-trends-agent",
    requirements=load_requirements(),
    extra_packages=[
        "./app",
    ],
)

# log remote_app
logging.info(
    f"Deployed agent to Vertex AI Agent Engine successfully, resource name: {remote_app.resource_name}"
)

# Update the .env file with the new Agent Engine ID
update_env_file(remote_app.resource_name, ENV_FILE_PATH)
