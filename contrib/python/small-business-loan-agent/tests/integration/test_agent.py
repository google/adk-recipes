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

"""Smoke tests for the Small Business Loan Agent."""

import dotenv
import pytest
from google.adk.runners import InMemoryRunner
from google.genai.types import Part, UserContent

from small_business_loan_agent.agent import root_agent

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session", autouse=True)
def load_env():
    dotenv.load_dotenv()


async def _run_agent(user_input: str) -> str:
    """Helper to run the agent and return the final response text."""
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
    return response


@pytest.mark.asyncio
async def test_agent_responds():
    """Agent should produce a non-empty response to a basic loan query."""
    response = await _run_agent(
        "Process this loan application for SBL-2025-00142"
    )
    assert response, "Agent returned an empty response"


@pytest.mark.asyncio
async def test_status_check():
    """Agent should handle a status check request."""
    response = await _run_agent("What is the status on SBL-2025-12345")
    assert response, "Agent returned an empty response"
