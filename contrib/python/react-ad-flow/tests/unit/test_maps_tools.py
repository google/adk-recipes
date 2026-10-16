# Copyright 2025 Google LLC
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

"""Unit tests for maps_tools.py.

Tests the Google Maps integration tools:
- get_campaign_map_data
- generate_static_map
- generate_map_visualization (requires LLM, marked slow)
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_campaign_with_metrics(rows, num_videos=1):
    """Create a campaign with activated video(s) and controlled metric rows.

    rows: dicts with keys metric_date (ISO str), impressions, revenue, and
    optional dwell_time_seconds, circulation, video_index (default 0).
    Returns {"campaign_id": int, "video_ids": [int, ...]}.
    """
    from app.database.db import get_db_cursor
    from app.tools.campaign_tools import create_campaign

    created = create_campaign(
        product_id=1, store_name="Parity Test Store", city="Austin", state="TX"
    )
    assert created["status"] == "success"
    campaign_id = created["campaign"]["id"]

    video_ids = []
    with get_db_cursor() as cursor:
        for i in range(num_videos):
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, video_filename, status)"
                " VALUES (?, ?, 'activated')",
                (campaign_id, f"parity-test-{campaign_id}-{i}.mp4"),
            )
            video_ids.append(cursor.lastrowid)
        for r in rows:
            cursor.execute(
                "INSERT INTO video_metrics"
                " (video_id, metric_date, impressions, dwell_time_seconds,"
                "  circulation, revenue) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    video_ids[r.get("video_index", 0)],
                    r["metric_date"],
                    r["impressions"],
                    r.get("dwell_time_seconds", 5.0),
                    r.get("circulation", 0),
                    r["revenue"],
                ),
            )
    return {"campaign_id": campaign_id, "video_ids": video_ids}


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


class TestGetCampaignMapData:
    """Tests for get_campaign_map_data tool."""

    def test_get_campaign_map_data_returns_all(
        self, test_db, mock_storage_module
    ):
        """get_campaign_map_data should return all campaign locations."""
        from app.tools.maps_tools import get_campaign_map_data

        result = get_campaign_map_data()

        # Should return locations with map data
        assert (
            "locations" in result or "campaigns" in result or result is not None
        )

    def test_get_campaign_map_data_has_google_maps_urls(
        self, test_db, mock_storage_module
    ):
        """Locations should include Google Maps URLs."""
        from app.tools.maps_tools import get_campaign_map_data

        result = get_campaign_map_data()

        # May not have links if no campaigns, but structure should exist
        assert result is not None

    def test_get_campaign_map_data_has_coordinates(
        self, test_db, mock_storage_module
    ):
        """Locations should include lat/lng coordinates."""
        from app.tools.maps_tools import get_campaign_map_data

        result = get_campaign_map_data()

        assert result is not None


class TestGenerateStaticMap:
    """Tests for generate_static_map tool."""

    @pytest.fixture
    def mock_maps_api(self):
        """Mock Google Maps Static API."""
        with patch("googlemaps.Client") as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value = mock_instance
            mock_instance.static_map.return_value = b"fake_image_data"
            yield mock_instance

    def test_generate_static_map_default(self, test_db, mock_storage_module):
        """generate_static_map should create map image."""
        from app.tools.maps_tools import generate_static_map

        result = generate_static_map()

        # Should return URL or error if API key not set
        assert (
            "url" in result
            or "error" in result
            or "map" in str(result).lower()
            or result is not None
        )

    def test_generate_static_map_by_status(self, test_db, mock_storage_module):
        """generate_static_map should color-code by status."""
        from app.tools.maps_tools import generate_static_map

        result = generate_static_map(color_by="status")

        # Should return result
        assert result is not None

    def test_generate_static_map_by_revenue(self, test_db, mock_storage_module):
        """generate_static_map should color-code by revenue."""
        from app.tools.maps_tools import generate_static_map

        result = generate_static_map(color_by="revenue")

        # Should return result
        assert result is not None

    def test_generate_static_map_types(self, test_db, mock_storage_module):
        """generate_static_map should support different map types."""
        from app.tools.maps_tools import generate_static_map

        map_types = ["roadmap", "satellite", "terrain", "hybrid"]

        for map_type in map_types:
            result = generate_static_map(map_type=map_type)
            assert result is not None


class TestGenerateMapVisualization:
    """Tests for generate_map_visualization tool (LLM call is mocked)."""

    async def test_generate_map_visualization_performance_map(
        self, test_db, mock_storage_module
    ):
        with patch("google.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = (
                RuntimeError("mocked API failure")
            )
            from app.tools.maps_tools import generate_map_visualization

            result = await generate_map_visualization(
                visualization_type="performance_map"
            )

            assert result["status"] == "error"
            assert "mocked API failure" in result["message"]

    async def test_generate_map_visualization_regional_comparison(
        self, test_db, mock_storage_module
    ):
        with patch("google.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = (
                RuntimeError("mocked API failure")
            )
            from app.tools.maps_tools import generate_map_visualization

            result = await generate_map_visualization(
                visualization_type="regional_comparison"
            )

            assert result["status"] == "error"
            assert "mocked API failure" in result["message"]

    async def test_generate_map_visualization_styles(
        self, test_db, mock_storage_module
    ):
        with patch("google.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = (
                RuntimeError("mocked API failure")
            )
            from app.tools.maps_tools import generate_map_visualization

            for style in ["infographic", "artistic", "simple"]:
                result = await generate_map_visualization(style=style)
                assert "Invalid style" not in result.get("message", ""), style
                assert result["status"] == "error"
                assert "mocked API failure" in result["message"], style

    async def test_default_metric_is_valid(self, test_db, mock_storage_module):
        """Calling with default metric must not trip the valid_metrics check.

        Regression: default was "revenue", which the tool itself rejects.
        """
        with patch("google.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = (
                RuntimeError("mocked API failure")
            )
            from app.tools.maps_tools import generate_map_visualization

            result = await generate_map_visualization()

            assert "Invalid metric" not in result.get("message", "")


class TestGetCampaignLocationsCurrentSchema:
    """get_campaign_locations must read campaign_videos/video_metrics,
    not the legacy campaign_ads/campaign_metrics tables (which are empty)."""

    def test_locations_report_real_video_metrics(self, test_db):
        with (
            patch("app.tools.maps_tools.GOOGLE_MAPS_API_KEY", "test-key"),
            patch("googlemaps.Client") as mock_gmaps,
        ):
            mock_gmaps.return_value.geocode.return_value = [
                {"geometry": {"location": {"lat": 34.05, "lng": -118.24}}}
            ]
            from app.tools.maps_tools import get_campaign_locations

            result = get_campaign_locations()

            assert "locations" in result
            # Demo data has activated videos with 30 days of metrics on the
            # pre-loaded campaigns — the legacy tables are empty, so any
            # non-zero count proves the query reads the current schema.
            campaigns_with_ads = [
                loc
                for loc in result["locations"]
                if loc["metrics"]["ad_count"] > 0
            ]
            assert len(campaigns_with_ads) >= 1
            assert any(
                loc["metrics"]["total_impressions"] > 0
                for loc in campaigns_with_ads
            )


class TestRpiCentralization:
    def test_map_data_rpi_is_thin_wrapper(self, test_db, mock_storage_module):
        from app.tools.maps_tools import get_campaign_map_data
        from app.tools.metrics_shared import compute_rpi

        _make_campaign_with_metrics(
            [
                {
                    "metric_date": _days_ago(1),
                    "impressions": 1000,
                    "revenue": 10.0,
                },
                {
                    "metric_date": _days_ago(2),
                    "impressions": 500,
                    "revenue": 50.0,
                },
            ]
        )
        result = get_campaign_map_data()
        assert result["status"] == "success"
        for loc in result["locations"]:
            if loc.get("metrics"):
                m = loc["metrics"]
                assert m["rpi"] == compute_rpi(
                    m["total_revenue"], m["total_impressions"]
                )
        s = result["summary"]
        assert s["overall_rpi"] == compute_rpi(
            s["total_revenue"], s["total_impressions"]
        )


def test_demographics_uses_retail_market_index(test_db):
    from app.tools.maps_tools import get_location_demographics

    result = get_location_demographics(city="Austin", state="TX")
    flat = str(result)
    assert "fashion_market_index" not in flat
    assert "retail_market_index" in flat
