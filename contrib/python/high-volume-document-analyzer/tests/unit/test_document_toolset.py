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

"""Unit tests for the document pagination logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.tools import ToolContext

from app.tools.document_toolset import (
    analyze_document_next_chunk,
)


@pytest.fixture
def mock_tool_context():
    context = ToolContext(invocation_context=MagicMock())
    return context


@pytest.mark.asyncio
async def test_analyze_document_reset_search(mock_tool_context):
    """Test if reset_search correctly resets the current_idx to 0 in ToolContext."""
    collection_id = "case123"
    sort_order = "asc"
    state_idx_key = f"idx_{collection_id}_{sort_order}"

    # Pre-fill bad state
    mock_tool_context.state[state_idx_key] = 50

    # Patch fetch to return empty list so it aborts early
    with patch(
        "app.tools.document_toolset.fetch_document_urls_async",
        new_callable=AsyncMock,
    ) as mock_fetch:
        mock_fetch.return_value = []

        res = await analyze_document_next_chunk(
            tool_context=mock_tool_context,
            collection_id=collection_id,
            question="What is this?",
            sort_order=sort_order,
            reset_search=True,
        )

        # It should have reset idx to 0 before fetching
        assert mock_tool_context.state[state_idx_key] == 0
        assert isinstance(res, dict)
        assert res.get("status") == "finished"
        assert "No documents found" in res.get("content", "")
