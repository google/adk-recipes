# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADK agent grounded in Gemini Enterprise / Agent Platform Search."""

import os
from collections.abc import Callable

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import VertexAiSearchTool
from google.genai import types


def build_data_store_path() -> str:
    """Build the Discovery Engine datastore path from environment values."""
    project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
    data_store_region = os.environ["DATA_STORE_REGION"]
    data_store_collection = os.environ["DATA_STORE_COLLECTION"]
    data_store_id = os.environ["DATA_STORE_ID"]
    return (
        f"projects/{project_id}/locations/{data_store_region}"
        f"/collections/{data_store_collection}/dataStores/{data_store_id}"
    )


def create_search_tool() -> VertexAiSearchTool | Callable[[str], str]:
    """Create the live datastore search tool or a deterministic test double."""
    if os.environ["INTEGRATION_TEST"] == "TRUE":

        def mock_search(query: str) -> str:
            """Return a deterministic search result for tests."""
            return f"Mock Gemini Enterprise datastore result for: {query}"

        return mock_search

    return VertexAiSearchTool(data_store_id=build_data_store_path())


GITHUB_CONTEXT_INSTRUCTION = """
You answer GitHub issue and pull-request questions using the connected Gemini
Enterprise datastore.

Rules:
1. Ground answers in datastore search results whenever the question needs
   product, policy, support, or internal knowledge.
2. If the datastore does not contain enough information, say what is missing
   and ask for the specific document or detail needed.
3. Keep replies concise and suitable for a GitHub comment.
4. Do not invent repository state, customer data, or credentials.
"""


root_agent = Agent(
    name="github_gemini_enterprise_agent",
    model=Gemini(
        model=os.environ["MODEL_NAME"],
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Answers GitHub comments using a Gemini Enterprise datastore through "
        "Agent Platform Search."
    ),
    instruction=GITHUB_CONTEXT_INSTRUCTION,
    tools=[create_search_tool()],
)

app = App(root_agent=root_agent, name="github_gemini_enterprise_app")
