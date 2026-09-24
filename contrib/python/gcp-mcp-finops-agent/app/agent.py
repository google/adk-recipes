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
"""FinOps agent on the Google-hosted BigQuery MCP server."""

from datetime import UTC, datetime

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app import config
from app.mcp_tools import bigquery_toolset
from app.prompt import build_instruction


def set_current_date(callback_context: CallbackContext) -> None:
    """Puts today's date in session state for the instruction."""
    callback_context.state["current_date_utc"] = datetime.now(UTC).strftime(
        "%Y-%m-%d"
    )


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the Agent."""
    return Agent(
        name="root_agent",
        model=Gemini(
            model=config.MODEL_NAME,
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description=(
            "Answers Google Cloud cost questions from the Cloud Billing"
            " export in BigQuery."
        ),
        instruction=build_instruction(
            config.BILLING_TABLE, config.BILLING_PROJECT
        ),
        tools=[bigquery_toolset()],
        before_agent_callback=set_current_date,
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
