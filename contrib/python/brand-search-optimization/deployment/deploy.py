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

import vertexai
from absl import app, flags
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp

from brand_search_optimization.agent import root_agent
from brand_search_optimization.shared_libraries import constants

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

    extra_packages = ["./brand_search_optimization"]

    remote_agent = agent_engines.create(
        adk_app,
        requirements=[
            "google-adk>=2.6.0,<3.0.0",
            "google-cloud-aiplatform[agent_engines]>=1.93.0",
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
    location = FLAGS.location if FLAGS.location else constants.LOCATION
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
