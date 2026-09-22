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

"""Unit tests for brand search optimization tools."""

from unittest.mock import MagicMock, patch

import pytest
from google.adk.tools.computer_use.base_computer import (
    BaseComputer,
    ComputerEnvironment,
)
from google.adk.tools.computer_use.computer_use_toolset import (
    ComputerUseToolset,
)
from starlette.datastructures import Headers

from brand_search_optimization.app_utils import (
    a2a,
    reasoning_engine_adapter,
    services,
)
from brand_search_optimization.shared_libraries import constants
from brand_search_optimization.sub_agents.comparison.models import (
    TitleOptimizationReport,
    TitleRecommendation,
)
from brand_search_optimization.tools import bq_connector
from brand_search_optimization.tools.browser_computer import (
    MockBrowserComputer,
    PlaywrightBrowserComputer,
    _calculate_scroll_deltas,
    _format_url,
    _validate_navigation_url,
    get_browser_computer,
    get_computer_use_toolset,
    validate_navigation_target,
)


class TestBigQueryConnector:
    """Tests for BigQuery catalog extraction tool."""

    @patch("brand_search_optimization.tools.bq_connector.client")
    def test_get_product_details_for_brand_success(self, mock_client):
        mock_row1 = MagicMock(
            Title="Cymbal Air Max",
            Description="Comfortable running shoes",
            Attributes="Size: 10, Color: Blue",
            Brand="Cymbal",
        )
        mock_row2 = MagicMock(
            Title="Cymbal Sportswear T-Shirt",
            Description="Cotton blend, short sleeve",
            Attributes="Size: L, Color: Black",
            Brand="Cymbal",
        )
        mock_results = [mock_row1, mock_row2]

        mock_query_job = MagicMock()
        mock_query_job.result.return_value = mock_results
        mock_client.query.return_value = mock_query_job

        with (
            patch.object(constants, "PROJECT", "test_project"),
            patch.object(constants, "TABLE_ID", "test_table"),
        ):
            response = bq_connector.get_product_details_for_brand(
                brand="Cymbal", limit=5
            )
            mock_client.query.assert_called_once()
            called_query = mock_client.query.call_args[0][0]
            assert "test_project" in called_query
            assert "test_table" in called_query
            assert response.brand == "Cymbal"
            assert response.total_count == 2
            assert len(response.products) == 2
            assert response.products[0].title == "Cymbal Air Max"
            assert response.products[1].title == "Cymbal Sportswear T-Shirt"
            assert response.is_sample_data is False

    @patch("brand_search_optimization.tools.bq_connector.client")
    def test_get_product_details_for_brand_null_fields(self, mock_client):
        mock_row = MagicMock()
        mock_row.Title = "Cymbal Runner"
        mock_row.title = None
        mock_row.Description = None
        mock_row.description = None
        mock_row.Attributes = None
        mock_row.attributes = None
        mock_row.Brand = None
        mock_row.brand = None

        mock_query_job = MagicMock()
        mock_query_job.result.return_value = [mock_row]
        mock_client.query.return_value = mock_query_job

        response = bq_connector.get_product_details_for_brand(
            brand="Cymbal", limit=5
        )
        assert response.total_count == 1
        assert response.products[0].title == "Cymbal Runner"
        assert response.products[0].description == "N/A"
        assert response.products[0].attributes == "N/A"
        assert response.products[0].brand == "Cymbal"

    def test_get_product_details_for_brand_empty(self):
        response = bq_connector.get_product_details_for_brand(brand="")
        assert response.total_count == 0
        assert response.products == []
        assert response.is_sample_data is False

    @patch("brand_search_optimization.tools.bq_connector.client", None)
    @patch(
        "brand_search_optimization.tools.bq_connector._get_client",
        return_value=None,
    )
    def test_get_product_details_for_brand_offline_fallback(self, _):
        response = bq_connector.get_product_details_for_brand(brand="Acme")
        assert response.brand == "Acme"
        assert response.total_count >= 1
        assert any("Acme" in p.title for p in response.products)
        assert response.is_sample_data is True


class TestBrowserComputer:
    """Tests for Computer Use browser automation."""

    def test_calculate_scroll_deltas(self):
        assert _calculate_scroll_deltas("up", 100) == (0, -100)
        assert _calculate_scroll_deltas("down", 100) == (0, 100)
        assert _calculate_scroll_deltas("left", 50) == (-50, 0)
        assert _calculate_scroll_deltas("right", 50) == (50, 0)

    def test_validate_navigation_url(self):
        # Valid URLs
        assert (
            _validate_navigation_url("https://www.google.com/search?q=shoes")
            is True
        )
        assert _validate_navigation_url("http://example.com/products") is True

        # Invalid / SSRF targets
        assert _validate_navigation_url("file:///etc/passwd") is False
        assert _validate_navigation_url("javascript:alert(1)") is False
        assert _validate_navigation_url("http://localhost:8080/secret") is False
        assert (
            _validate_navigation_url("http://127.0.0.1:8000/internal") is False
        )
        assert (
            _validate_navigation_url("http://169.254.169.254/latest/meta-data/")
            is False
        )
        assert (
            _validate_navigation_url(
                "http://metadata.google.internal/computeMetadata/v1/"
            )
            is False
        )
        assert _validate_navigation_url("http://10.0.0.1/admin") is False
        assert _validate_navigation_url("http://192.168.1.1/") is False
        assert _validate_navigation_url("invalid-url") is False

    def test_validate_navigation_url_rejects_parser_confusion(self):
        # Chromium strips tab/CR/LF before parsing, so the host it reaches
        # differs from the one urllib.parse reports.
        assert (
            _validate_navigation_url("http://metadata.google\t.internal/")
            is False
        )
        assert _validate_navigation_url("http://127.0.0\n.1/admin") is False

        # Chromium treats a backslash as an authority separator.
        assert (
            _validate_navigation_url("http://www.google.com\\@127.0.0.1/")
            is False
        )

        # Userinfo makes a blocked host read as a trusted one.
        assert (
            _validate_navigation_url(
                "http://www.google.com@metadata.google.internal/"
            )
            is False
        )

        # Non-ASCII hosts would be IDNA-encoded by the browser only.
        assert _validate_navigation_url("http://exämple.com/") is False

    def test_validate_navigation_url_rejects_trailing_root_label(self):
        # A trailing dot is the explicit DNS root label: it resolves to the
        # same host but would not match the blocklists without normalisation.
        assert (
            _validate_navigation_url(
                "http://metadata.google.internal./computeMetadata/v1/"
            )
            is False
        )
        assert _validate_navigation_url("http://localhost./secret") is False
        assert _validate_navigation_url("http://127.0.0.1./admin") is False
        assert _validate_navigation_url("http://foo.local./") is False
        assert _validate_navigation_url("http://10.0.0.1../") is False

        # A root label on a public host stays allowed.
        assert _validate_navigation_url("https://www.google.com./") is True

    @pytest.mark.asyncio
    async def test_validate_navigation_target_resolves_hostname(self):
        # A public-looking hostname that resolves to a private address is
        # rejected, and one that resolves publicly is allowed.
        with patch(
            "socket.getaddrinfo",
            return_value=[(None, None, None, "", ("10.0.0.7", 0))],
        ):
            assert (
                await validate_navigation_target("https://internal.example.com")
                is False
            )
        with patch(
            "socket.getaddrinfo",
            return_value=[(None, None, None, "", ("93.184.216.34", 0))],
        ):
            assert (
                await validate_navigation_target("https://example.com") is True
            )

    @pytest.mark.asyncio
    async def test_mock_browser_computer_operations(self):
        computer = MockBrowserComputer()

        size = await computer.screen_size()
        assert size == (1280, 800)

        env = await computer.environment()
        assert env == ComputerEnvironment.ENVIRONMENT_BROWSER

        state = await computer.open_web_browser()
        assert state.screenshot is not None
        assert len(state.screenshot) > 0
        import io

        from PIL import Image

        with Image.open(io.BytesIO(state.screenshot)) as img:
            img.load()
            assert img.format == "PNG"
            assert img.size == (1, 1)

        with patch(
            "brand_search_optimization.tools.browser_computer._resolved_addresses_allowed",
            return_value=True,
        ):
            nav_state = await computer.navigate(
                "https://www.google.com/search?tbm=shop&q=running+shoes"
            )
        assert "running+shoes" in nav_state.url

        # Disallowed navigation is rejected
        rejected_nav = await computer.navigate("http://127.0.0.1:8080/admin")
        assert "running+shoes" in rejected_nav.url

        type_state = await computer.type_text_at(
            x=100, y=200, text="kids sneakers"
        )
        assert "kids+sneakers" in type_state.url

        click_state = await computer.click_at(x=150, y=250)
        assert click_state.screenshot is not None

        scroll_state = await computer.scroll_document(direction="down")
        assert scroll_state.screenshot is not None

    def test_format_url_valid(self):
        url = _format_url("google.com/search?q=shoes")
        assert url == "https://google.com/search?q=shoes"
        assert _format_url("http://example.com") == "http://example.com"
        assert _format_url("https://example.com") == "https://example.com"

    def test_format_url_blocks_unsafe_schemes(self):
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            _format_url("file:///etc/passwd")
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            _format_url("javascript:alert(1)")

    @pytest.mark.asyncio
    async def test_mock_browser_computer_rejects_unsafe_schemes(self):
        computer = MockBrowserComputer()
        before = (await computer.current_state()).url
        # An unsupported scheme is reported back as an unchanged state rather
        # than raised, so the agent loop can recover.
        state = await computer.navigate("file:///etc/passwd")
        assert state.url == before

    @pytest.mark.asyncio
    async def test_playwright_browser_computer_key_combination(self):
        computer = PlaywrightBrowserComputer()
        mock_page = MagicMock()
        mock_page.keyboard.press = MagicMock()
        computer._pages["default"] = mock_page

        from unittest.mock import AsyncMock

        mock_page.keyboard.press = AsyncMock()
        mock_page.screenshot = AsyncMock(return_value=b"fake_screenshot")
        mock_page.url = "https://www.google.com"

        await computer.key_combination(["Control", "A"])
        mock_page.keyboard.press.assert_awaited_once_with("Control+A")

    def test_get_browser_computer_offline_flag(self):
        with patch.object(constants, "DISABLE_WEB_DRIVER", 1):
            computer = get_browser_computer()
            assert isinstance(computer, MockBrowserComputer)

    @pytest.mark.asyncio
    async def test_get_computer_use_toolset(self):
        toolset = get_computer_use_toolset()
        assert isinstance(toolset, ComputerUseToolset)
        assert toolset._computer is not None
        assert isinstance(toolset._computer, BaseComputer)
        tools = await toolset.get_tools()
        assert len(tools) > 0
        tool_names = {t.name for t in tools}
        assert "navigate" in tool_names
        assert "click_at" in tool_names
        assert "type_text_at" in tool_names


class TestModels:
    """Tests for Pydantic optimization schemas."""

    def test_title_optimization_report_schema(self):
        rec = TitleRecommendation(
            original_title="Runner",
            proposed_title="Brand Men's Breathable Mesh Pro Runner - Black Size 10",
            keywords_added=["Men's", "Breathable Mesh", "Pro Runner"],
            searchability_score=94.5,
            rationale="Captures gender, material, and category attributes.",
        )
        report = TitleOptimizationReport(
            brand="Brand",
            primary_search_keyword="running shoes",
            competitor_title_patterns=[
                "[Brand] [Gender] [Material] [Model] [Category]"
            ],
            keyword_gaps=["breathable mesh", "men's"],
            recommendations=[rec],
            summary_findings="Significant discovery score improvement expected.",
        )
        assert report.brand == "Brand"
        assert report.recommendations[0].searchability_score == 94.5


class TestAppUtils:
    """Tests for app serving adapters, services, and A2A helpers."""

    def test_no_op_instrumentor_builder(self):
        assert (
            reasoning_engine_adapter._no_op_instrumentor_builder("test-proj")
            is None
        )

    @pytest.mark.asyncio
    async def test_invoke_method_sync_and_async(self):
        def sync_fn(x: int, y: int = 1) -> int:
            return x + y

        async def async_fn(x: int, y: int = 2) -> int:
            return x * y

        sync_res = await reasoning_engine_adapter._invoke_method(
            sync_fn, {"input": {"x": 5, "y": 3}}
        )
        assert sync_res == 8

        async_res = await reasoning_engine_adapter._invoke_method(
            async_fn, {"input": {"x": 4, "y": 3}}
        )
        assert async_res == 12

    def test_services_shared_recursion_prevention(self):
        with patch.dict(
            "os.environ",
            {
                "SESSION_SERVICE_URI": "shared://session",
                "ARTIFACT_SERVICE_URI": "shared://artifact",
            },
            clear=False,
        ):
            services.get_session_service.cache_clear()
            services.get_artifact_service.cache_clear()
            session_svc = services.get_session_service()
            artifact_svc = services.get_artifact_service()
            assert session_svc is not None
            assert artifact_svc is not None

    def test_a2a_context_builder_version_resolution(self):
        builder = a2a._A2AServerCallContextBuilder()

        # When headers contain version
        mock_req_with_header = MagicMock()
        mock_req_with_header.scope = {}
        mock_req_with_header.headers = Headers({"A2A-Version": "0.3"})
        ctx1 = builder.build(mock_req_with_header)
        assert ctx1.state["headers"]["A2A-Version"] == "0.3"

        # When header missing but method has '/'
        mock_req_03 = MagicMock()
        mock_req_03.scope = {}
        mock_req_03.headers = Headers({})
        mock_req_03._json = {"method": "message/send"}
        ctx2 = builder.build(mock_req_03)
        assert ctx2.state["headers"]["A2A-Version"] == "0.3"

        # When header missing and method is 1.0 format
        mock_req_10 = MagicMock()
        mock_req_10.scope = {}
        mock_req_10.headers = Headers({})
        mock_req_10._json = {"method": "SendMessage"}
        ctx3 = builder.build(mock_req_10)
        assert ctx3.state["headers"]["A2A-Version"] == "1.0"
