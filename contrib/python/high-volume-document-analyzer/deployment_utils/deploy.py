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

import json
import os
import sys

import vertexai
from dotenv import load_dotenv
from vertexai import agent_engines

# Add the project root to sys.path to allow importing app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.agent import root_agent

load_dotenv(override=True)
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
STAGING_BUCKET = os.getenv("STAGING_BUCKET")

if not STAGING_BUCKET:
    print("Error: STAGING_BUCKET environment variable is not set.")
    sys.exit(1)

vertexai.init(
    project=PROJECT_ID,
    location=LOCATION,
    staging_bucket=STAGING_BUCKET
    if STAGING_BUCKET.startswith("gs://")
    else f"gs://{STAGING_BUCKET}",
)

adk_app = agent_engines.AdkApp(
    agent=root_agent,
    enable_tracing=True,
)

if os.path.exists(".agent_engine_resource.json"):
    with open(".agent_engine_resource.json") as f:
        app_resource_data = json.load(f)
        existing_resource_name = app_resource_data.get("resource_name")
else:
    existing_resource_name = None

common_args = {
    "agent_engine": adk_app,
    "extra_packages": [
        "app/__init__.py",
        "app/agent.py",
        "app/prompt.py",
        "app/tools/__init__.py",
        "app/tools/document_toolset.py",
        "app/tools/process_toolset.py",
    ],
    "requirements": [
        "google-adk>=1.28.0",
        "google-cloud-aiplatform[adk,agent-engines]>=1.93.0",
        "opentelemetry-instrumentation-google-genai>=0.4b0",
        "python-dotenv>=1.0.1",
        "reportlab>=4.2.0",
        "pypdf>=6.17.0",
        "requests>=2.33.0",
        "pandas>=2.3.3",
        "beautifulsoup4>=4.14.3",
        "aiohttp>=3.14.3",
        "google-cloud-secret-manager>=2.22.0",
    ],
    "env_vars": {
        "GOOGLE_CLOUD_PROJECT": PROJECT_ID,
        "GOOGLE_CLOUD_LOCATION": LOCATION,
        "GOOGLE_GENAI_USE_VERTEXAI": os.getenv("GOOGLE_GENAI_USE_VERTEXAI"),
        "USE_MOCK_API": os.getenv("USE_MOCK_API"),
        "CLIENT_ID": os.getenv("CLIENT_ID"),
        "CLIENT_SECRET": os.getenv("CLIENT_SECRET"),
        "URL_TOKEN_API_URL": os.getenv("URL_TOKEN_API_URL"),
        "DOCUMENT_API_BASE_URL": os.getenv("DOCUMENT_API_BASE_URL"),
        "MODEL_NAME_DOC_PROCESSING": os.getenv("MODEL_NAME_DOC_PROCESSING"),
        "MODEL_NAME_AGENT": os.getenv("MODEL_NAME_AGENT"),
        "BATCH_SIZE": os.getenv("BATCH_SIZE"),
        "MAX_CONCURRENT_DOWNLOADS": os.getenv("MAX_CONCURRENT_DOWNLOADS"),
    },
    "display_name": os.getenv("AGENT_DISPLAY_NAME"),
    "description": "Agent that analyzes large collections of documents in batches to answer questions and provide summaries.",
}

try:
    if existing_resource_name:
        print(
            f"Updating existing Agent Engine resource: {existing_resource_name}"
        )
        remote_app = agent_engines.update(
            resource_name=existing_resource_name,
            **common_args,
        )
    else:
        print("Creating new Agent Engine resource")
        remote_app = agent_engines.create(
            **common_args,
        )

    print("Deployment finished!")

    app_resource_data = {"resource_name": remote_app.resource_name}
    with open(".agent_engine_resource.json", "w") as f:
        json.dump(app_resource_data, f, indent=2)

    print(f"Resource Name: {remote_app.resource_name}")
except Exception as e:
    print(f"Deployment failed: {e}")
    sys.exit(1)
