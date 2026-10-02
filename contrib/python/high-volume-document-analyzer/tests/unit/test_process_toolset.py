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

"""Unit tests for the process API toolset logic."""

from unittest.mock import patch

import pytest

from app.tools.process_toolset import (
    fetch_document_urls_async,
)


@pytest.mark.asyncio
async def test_fetch_mock_urls():
    """Verify that when USE_MOCK_API is True, the mock URL list is predictably returned."""
    with patch("app.tools.process_toolset.USE_MOCK_API", True):
        urls = await fetch_document_urls_async("test_collection_123")
        assert len(urls) == 1
        assert "dummy.pdf" in urls[0]
        assert "w3.org" in urls[0]


@pytest.mark.asyncio
async def test_fetch_real_urls_empty_on_error():
    """Verify that an exception inside the real API call safely returns an empty list."""
    with patch(
        "app.tools.process_toolset.USE_MOCK_API",
        False,
    ):
        with patch(
            "app.tools.process_toolset.get_auth_token_async",
            side_effect=Exception("Mock Auth Failure"),
        ):
            urls = await fetch_document_urls_async("test_collection")
            assert urls == []
