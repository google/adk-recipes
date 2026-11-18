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

"""Deployment script for Brand Search Optimization agent."""

import os

import vertexai
from absl import app, flags
from vertexai import agent_engines
from vertexai.agent_engines.templates.adk import AdkApp

from app.agent import root_agent
from app.shared_libraries import constants

FLAGS = flags.FLAGS
flags.DEFINE_string("project_id", None, "GCP project ID.")
flags.DEFINE_string("location", None, "GCP location.")
flags.DEFINE_string("bucket", None, "GCP bucket.")
flags.DEFINE_string("resource_id", None, "ReasoningEngine resource ID.")
flags.DEFINE_bool("create", False, "Create a new agent.")
flags.DEFINE_bool("delete", False, "Delete an existing agent.")
flags.mark_bool_flags_as_mutual_exclusive(["create", "delete"])


def create(env_vars: dict) -> None:
    adk_app = AdkApp(
        agent=root_agent,
        enable_tracing=True,
    )

    extra_packages = ["./app"]

    remote_agent = agent_engines.create(
        adk_app,
        requirements=[
            "google-adk>=2.13.0,<3.0.0",
            "google-cloud-aiplatform[agent_engines]>=1.156.0,<3.0.0",
            "pydantic>=2.10.0",
            "requests",
            "python-dotenv",
            "google-genai",
            "playwright",
            "google-cloud-bigquery",
            "absl-py",
            "pillow",
        ],
        extra_packages=extra_packages,
        env_vars=env_vars,
    )
    print(f"Created remote agent: {remote_agent.resource_name}")


def delete(resource_id: str) -> None:
    remote_agent = agent_engines.get(resource_id)
    remote_agent.delete(force=True)
    print(f"Deleted remote agent: {resource_id}")


def main(argv: list[str]) -> None:
    project_id = FLAGS.project_id if FLAGS.project_id else constants.PROJECT
    # Reasoning Engine requires a regional location (e.g., us-central1) and
    # rejects 'global' as lightweight-only.
    location = (
        FLAGS.location
        or os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
        or "us-central1"
    )
    bucket = FLAGS.bucket if FLAGS.bucket else constants.STAGING_BUCKET
    env_vars = {}

    print(f"PROJECT: {project_id}")
    print(f"LOCATION: {location}")
    print(f"BUCKET: {bucket}")

    if not project_id:
        print("Missing required environment variable: GOOGLE_CLOUD_PROJECT")
        return
    elif not location:
        print("Missing required environment variable: GOOGLE_CLOUD_LOCATION")
        return
    elif not bucket:
        print("Missing required environment variable: STAGING_BUCKET")
        return

    env_vars["DISABLE_WEB_DRIVER"] = (
        "1" if constants.DISABLE_WEB_DRIVER else "0"
    )
    if constants.MODEL:
        env_vars["MODEL"] = constants.MODEL
    # Forward model endpoint location (e.g. 'global' for Gemini 3.5 models)
    # to the remote Reasoning Engine instance.
    if constants.LOCATION:
        env_vars["GOOGLE_CLOUD_LOCATION"] = constants.LOCATION

    vertexai.init(
        project=project_id,
        location=location,
        staging_bucket=f"gs://{bucket}",
    )

    if FLAGS.create:
        create(env_vars)
    elif FLAGS.delete:
        if not FLAGS.resource_id:
            print("resource_id is required for delete")
            return
        delete(FLAGS.resource_id)
    else:
        print("Unknown command")


if __name__ == "__main__":
    app.run(main)
