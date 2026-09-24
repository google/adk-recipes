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
"""Integration tests. Need credentials and the setup in README.md."""

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types

from app import config
from app.agent import create_agent
from app.mcp_tools import bigquery_toolset


@pytest.mark.asyncio
async def test_server_offers_every_allowlisted_tool() -> None:
    toolset = bigquery_toolset()
    try:
        names = {tool.name for tool in await toolset.get_tools()}
    finally:
        await toolset.close()

    assert names == set(config.READONLY_BIGQUERY_TOOLS)


@pytest.mark.asyncio
async def test_agent_answers_from_a_query() -> None:
    runner = InMemoryRunner(agent=create_agent(), app_name="app")
    session = await runner.session_service.create_session(
        app_name="app", user_id="test_user"
    )
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text="What date range does the billing data cover?"
            )
        ],
    )

    tool_calls: list[str] = []
    answer = ""
    async for event in runner.run_async(
        user_id="test_user", session_id=session.id, new_message=message
    ):
        parts = event.content.parts if event.content else None
        for part in parts or []:
            if part.function_call:
                tool_calls.append(part.function_call.name)
            if part.text:
                answer += part.text
    await runner.close()

    assert "execute_sql_readonly" in tool_calls
    assert answer
