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

"""Integration tests for the SDLC Workflow Suite agents."""

import textwrap

import dotenv
import pytest
from google.adk.runners import InMemoryRunner
from google.genai.types import Part, UserContent

from sdlc_workflow_suite.agent import (
    root_agent,
    task_planner_agent,
    technical_designer_agent,
    user_story_refiner_agent,
)

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session", autouse=True)
def load_env():
    dotenv.load_dotenv()


@pytest.fixture
def create_runner_and_session():
    """Creates an InMemoryRunner and establishes an active test session for an agent."""

    async def _create(agent):
        runner = InMemoryRunner(agent=agent)
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="test_user"
        )
        return runner, session

    return _create


@pytest.mark.asyncio
async def test_user_story_refiner_happy_path(create_runner_and_session):
    """Runs the user story refiner agent on a sparse draft and expects an expanded story."""
    user_input = "We need users to be able to log in with Google SSO."

    runner, session = await create_runner_and_session(user_story_refiner_agent)
    content = UserContent(parts=[Part(text=user_input)])
    response = ""
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response += part.text

    assert response != ""
    assert (
        "user story" in response.lower()
        or "acceptance criteria" in response.lower()
    )


@pytest.mark.asyncio
async def test_technical_designer_happy_path(create_runner_and_session):
    """Runs the technical designer agent on a simple input and expects a valid RFC design."""
    user_input = textwrap.dedent("""
        I want to build a simple user authentication service with Flask and PostgreSQL.
        Please provide a high-level technical design for this.
        """).strip()

    runner, session = await create_runner_and_session(technical_designer_agent)
    content = UserContent(parts=[Part(text=user_input)])
    response = ""
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response += part.text

    assert "flask" in response.lower()
    assert "postgresql" in response.lower()


@pytest.mark.asyncio
async def test_task_planner_happy_path(create_runner_and_session):
    """Runs the task planner agent and verifies the comprehensive task table output."""
    user_input = textwrap.dedent(
        """Here is the user story and technical design document:
        User Story: As a user, I want to be able to reset my password so that I can regain access to my account if I forget it.

        Technical Design:
        1. Create a new endpoint `/api/forgot-password` that accepts an email address and sends a password reset link.
        2. Create a new endpoint `/api/reset-password` that accepts a reset token and a new password, and updates the user's password in the database.
        3. Database: Add a `reset_token` and `reset_token_expires_at` column to the `users` table.
        """
    ).strip()

    runner, session = await create_runner_and_session(task_planner_agent)
    content = UserContent(parts=[Part(text=user_input)])
    response = ""
    artifact_content = ""
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=content,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response += part.text
                if (
                    part.function_call
                    and part.function_call.name == "save_artifact"
                    and part.function_call.args
                ):
                    artifact_content = part.function_call.args.get(
                        "content", ""
                    )

    content_to_check = artifact_content if artifact_content else response
    assert "Task ID" in content_to_check
    assert "Technical Description & Files" in content_to_check
    assert "Acceptance Criteria & Testing" in content_to_check
    assert response != ""


@pytest.mark.asyncio
async def test_sequential_agent_happy_path(create_runner_and_session):
    """Runs the full SDLC SequentialAgent pipeline."""
    user_input = textwrap.dedent("""
        We need an audit log endpoint for compliance that records user sign-in events.
        """).strip()

    runner, session = await create_runner_and_session(root_agent)
    content = UserContent(parts=[Part(text=user_input)])
    events = []
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=content,
    ):
        events.append(event)

    assert len(events) > 0
