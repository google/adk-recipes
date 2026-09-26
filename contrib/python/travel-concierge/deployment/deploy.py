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

"""Deployment script for Travel Concierge."""

import os

import vertexai
from absl import app, flags
from dotenv import load_dotenv
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp

from travel_concierge.agent import root_agent

FLAGS = flags.FLAGS
flags.DEFINE_string("project_id", None, "GCP project ID.")
flags.DEFINE_string("location", None, "GCP location.")
flags.DEFINE_string("bucket", None, "GCP bucket.")
flags.DEFINE_string("model_endpoint", None, "GCP model endpoint.")

flags.DEFINE_string(
    "initial_states_path",
    None,
    "Relative path to the initial state file, .e.g eval/itinerary_empty_default.json",
)
flags.DEFINE_string("map_key", None, "API Key for Google Maps Grounding API")

flags.DEFINE_string("resource_id", None, "ReasoningEngine resource ID.")
flags.DEFINE_bool("create", False, "Creates a new deployment.")
flags.DEFINE_bool("quicktest", False, "Try a new deployment with one turn.")
flags.DEFINE_bool("delete", False, "Deletes an existing deployment.")
flags.mark_bool_flags_as_mutual_exclusive(["create", "delete", "quicktest"])


def create(env_vars: dict[str, str]) -> None:
    """Creates a new deployment."""
    print(env_vars)
    app = AdkApp(agent=root_agent, enable_tracing=True)

    remote_agent = agent_engines.create(
        app,
        display_name="Travel-Concierge-ADK",
        description="An Example AgentEngine Deployment",
        requirements=[
            "google-adk (>=1.31.0,<2.0.0)",
            "google-cloud-aiplatform[agent_engines] (>=1.157.0)",
            "google-genai (>=1.21.1,<2.0.0)",
            "absl-py (>=2.2.1,<3.0.0)",
            "pydantic (>=2.13.4,<3.0.0)",
            "requests (>=2.32.3,<3.0.0)",
            "python-dotenv>=1.0.1",
            "arize-otel>=0.8.2; python_version >= '3.11' and python_version < '3.13'",
            "openinference-instrumentation-google-adk>=0.1.0; python_version >= '3.11' and python_version < '3.14'",
            "openinference-instrumentation>=0.1.53",
            "arize>=8.35.0",
            "arize-phoenix-evals>=3.1.0",
            "scikit-learn>=1.9.0",
            "pandas>=2.3.0",
        ],
        extra_packages=[
            "./travel_concierge",  # The main package
        ],
        env_vars=env_vars,
    )
    print(f"Created remote agent: {remote_agent.resource_name}")


def delete(resource_id: str) -> None:
    remote_agent = agent_engines.get(resource_id)
    remote_agent.delete(force=True)
    print(f"Deleted remote agent: {resource_id}")


def send_message(resource_id: str, message: str) -> None:
    """Send a message to the deployed agent."""
    remote_agent = agent_engines.get(resource_id)
    user_id = "traveler0115"
    session = remote_agent.create_session(user_id=user_id)
    print(f"Session successfully initialized. ID: {session['id']}")

    for event in remote_agent.stream_query(
        user_id=user_id,
        session_id=session["id"],
        message=message,
    ):
        print(event)

    print("Done.")


def main(argv: list[str]) -> None:
    load_dotenv(override=True)
    env_vars = {}

    project_id = (
        FLAGS.project_id
        if FLAGS.project_id
        else os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    model_endpoint = (
        FLAGS.model_endpoint
        if FLAGS.model_endpoint
        else os.getenv("GOOGLE_CLOUD_LOCATION")
    )
    location = (
        FLAGS.location if FLAGS.location else os.getenv("GOOGLE_DEPLOY_REGION")
    )
    bucket = (
        FLAGS.bucket
        if FLAGS.bucket
        else os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")
    )
    # Variables for Travel Concierge from .env
    initial_states_path = (
        FLAGS.initial_states_path
        if FLAGS.initial_states_path
        else os.getenv("TRAVEL_CONCIERGE_SCENARIO")
    )
    env_vars["TRAVEL_CONCIERGE_SCENARIO"] = initial_states_path
    map_key = (
        FLAGS.initial_states_path
        if FLAGS.initial_states_path
        else os.getenv("GOOGLE_MAPS_API_KEY")
    )
    env_vars["GOOGLE_MAPS_API_KEY"] = map_key

    model_string = os.getenv("GOOGLE_GENAI_MODEL")
    env_vars["GOOGLE_GENAI_MODEL"] = model_string

    print(f"PROJECT: {project_id}")
    print(f"LOCATION: {location}")
    print(f"BUCKET: {bucket}")
    print(f"INITIAL_STATE: {initial_states_path}")
    print(f"MAP: {map_key[:5]}")
    print(f"MODEL: {model_string}")
    print(f"MODEL ENDPOINT: {model_endpoint}")

    if not project_id:
        print("Missing required environment variable: GOOGLE_CLOUD_PROJECT")
        return
    elif not model_endpoint:
        print("Missing required environment variable: GOOGLE_CLOUD_LOCATION")
        return
    elif not location:
        print("Missing required environment variable: GOOGLE_DEPLOY_REGION")
        return
    elif location == "global":
        print(
            f'Deployment location cannot be "{location}", please rerun with --location <region>.'
        )
        return
    elif not bucket:
        print(
            "Missing required environment variable: GOOGLE_CLOUD_STORAGE_BUCKET"
        )
        return
    elif not initial_states_path:
        print(
            "Missing required environment variable: TRAVEL_CONCIERGE_SCENARIO"
        )
        return
    elif not map_key:
        print("Missing required environment variable: GOOGLE_MAPS_API_KEY")
        return
    elif not model_string:
        print("Missing required environment variable: GOOGLE_GENAI_MODEL")
        return

    vertexai.init(
        project=project_id,
        location=location,
        staging_bucket=f"gs://{bucket}",
    )

    # ADK uses this to determine model endpoint within AgentEngine
    env_vars["GOOGLE_CLOUD_LOCATION"] = model_endpoint

    if FLAGS.create:
        create(env_vars)
    elif FLAGS.delete:
        if not FLAGS.resource_id:
            print("resource_id is required for delete")
            return
        delete(FLAGS.resource_id)
    elif FLAGS.quicktest:
        if not FLAGS.resource_id:
            print("resource_id is required for quicktest")
            return
        send_message(
            FLAGS.resource_id,
            "Tell me more about activities I can do around Machu Picchu",
        )
    else:
        print("Unknown command")


if __name__ == "__main__":
    app.run(main)
