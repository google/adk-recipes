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

"""Integration test for the Google Trends agent.

Drives the full SequentialAgent pipeline through a real model and BigQuery.
Excluded from CI (needs credentials + a reachable model); run locally with
`uv run pytest tests/integration`.
"""

import dotenv
import pytest
from google.adk.runners import InMemoryRunner
from google.genai.types import Part, UserContent

from app.agent import root_agent

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session", autouse=True)
def load_env():
    dotenv.load_dotenv()


@pytest.mark.asyncio
async def test_happy_path():
    """Ask for trending terms and expect a non-empty, non-error response."""
    user_input = "What are the top trending keywords in Canada?"

    runner = InMemoryRunner(agent=root_agent)
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id="test_user"
    )
    content = UserContent(parts=[Part(text=user_input)])
    response = ""
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=content,
    ):
        if (
            event.content
            and event.content.parts
            and event.content.parts[0].text
        ):
            response = event.content.parts[0].text

    # The executor should return interpreted results, not a BigQuery error.
    assert response, "agent produced no textual response"
    assert "error executing bigquery query" not in response.lower()
    # A zero-row answer is a failure, not a pass: it means the generated SQL
    # pinned refresh_date to a day the public dataset has not published yet.
    assert "no results" not in response.lower(), (
        f"agent returned an empty result set: {response}"
    )
