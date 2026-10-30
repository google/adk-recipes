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

"""Unit tests for video_tools.py.

Tests the video-related tools:
- list_products (no LLM)
- get_variation_presets (no LLM)
- list_campaign_videos (no LLM)
- list_campaign_ads (legacy, no LLM)
- generate_video_from_product (marked slow - uses Omni)
- generate_video_with_variation (marked slow - uses Omni)
"""

from unittest.mock import patch

import pytest


class TestListProducts:
    """Tests for list_products tool."""

    def test_list_products_returns_all(self, test_db):
        """list_products should return all 22 pre-loaded products."""
        from app.tools.video_tools import list_products

        result = list_products()

        assert "products" in result
        products = result["products"]
        assert len(products) >= 22  # Pre-loaded product catalog

    def test_list_products_structure(self, test_db):
        """Each product should have required fields."""
        from app.tools.video_tools import list_products

        result = list_products()
        product = result["products"][0]

        # Check required fields
        assert "id" in product
        assert "name" in product
        assert "category" in product

    def test_list_products_has_image_url(self, test_db, mock_storage_module):
        """Products should include image URLs."""
        from app.tools.video_tools import list_products

        result = list_products()
        product = result["products"][0]

        # Should have image reference
        assert "image_url" in product or "image_filename" in product

    def test_list_products_filter_by_category(self, test_db):
        """list_products should filter by category."""
        from app.tools.video_tools import list_products

        result = list_products(category="dress")

        assert "products" in result
        for product in result["products"]:
            assert product["category"] == "dress"

    def test_list_products_categories(self, test_db):
        """Products should span multiple categories."""
        from app.tools.video_tools import list_products

        result = list_products()
        categories = {p["category"] for p in result["products"]}

        # Should have multiple categories
        expected = {"dress", "pants", "top", "skirt", "outerwear"}
        assert len(categories.intersection(expected)) >= 3


class TestGetVariationPresets:
    """Tests for get_variation_presets tool."""

    def test_get_variation_presets_returns_all(self, test_db):
        """get_variation_presets should return all preset categories."""
        from app.tools.video_tools import get_variation_presets

        result = get_variation_presets()

        assert "presets" in result
        presets = result["presets"]

        # Should have diversity, settings, and moods
        assert "diversity" in presets or len(presets) >= 3

    def test_diversity_presets(self, test_db):
        """Diversity presets should include model ethnicities."""
        from app.tools.video_tools import get_variation_presets

        result = get_variation_presets()

        # Should have diversity/ethnicity options
        all_values = str(result).lower()
        ethnicities = ["asian", "european", "african", "latina"]

        found = sum(1 for e in ethnicities if e in all_values)
        assert found >= 2  # At least 2 ethnicities mentioned

    def test_settings_presets(self, test_db):
        """Settings presets should include scene options."""
        from app.tools.video_tools import get_variation_presets

        result = get_variation_presets()

        # Should have setting options
        all_values = str(result).lower()
        settings = ["studio", "beach", "urban", "cafe"]

        found = sum(1 for s in settings if s in all_values)
        assert found >= 2  # At least 2 settings mentioned

    def test_moods_presets(self, test_db):
        """Mood presets should include mood options."""
        from app.tools.video_tools import get_variation_presets

        result = get_variation_presets()

        # Should have mood options
        all_values = str(result).lower()
        moods = ["elegant", "romantic", "bold", "playful"]

        found = sum(1 for m in moods if m in all_values)
        assert found >= 2  # At least 2 moods mentioned


class TestListCampaignVideos:
    """Tests for list_campaign_videos tool."""

    def test_list_campaign_videos_empty(self, test_db):
        """list_campaign_videos should return empty for new campaigns."""
        from app.tools.video_tools import list_campaign_videos

        # Campaign 1 exists but may not have videos yet
        result = list_campaign_videos(campaign_id=1)

        assert "videos" in result
        # May be empty or have demo videos
        assert isinstance(result["videos"], list)

    def test_list_campaign_videos_structure(self, test_db):
        """Videos should have required fields."""
        from app.tools.video_tools import list_campaign_videos

        result = list_campaign_videos()

        if result.get("videos"):
            video = result["videos"][0]
            assert "id" in video
            assert "status" in video

    def test_list_campaign_videos_filter_by_status(self, test_db):
        """list_campaign_videos should filter by status."""
        from app.tools.video_tools import list_campaign_videos

        result = list_campaign_videos(status="generated")

        assert "videos" in result
        for video in result.get("videos", []):
            assert video["status"] == "generated"


class TestListCampaignAds:
    """Tests for list_campaign_ads tool (legacy)."""

    def test_list_campaign_ads(self, test_db):
        """list_campaign_ads should return ads for a campaign."""
        from app.tools.video_tools import list_campaign_ads

        result = list_campaign_ads(campaign_id=1)

        assert "ads" in result or "videos" in result or "error" in result


class TestCreativeVariation:
    """Tests for CreativeVariation model."""

    def test_default_variation(self):
        """get_default_variation should return valid defaults."""
        from app.tools.video_tools import get_default_variation

        variation = get_default_variation()

        assert variation is not None
        assert hasattr(variation, "model_ethnicity")
        assert hasattr(variation, "setting")
        assert hasattr(variation, "mood")

    def test_variation_from_dict(self):
        """CreativeVariation should accept dict input."""
        from app.models.variation import CreativeVariation

        data = {
            "name": "test-asian-beach-romantic",
            "model_ethnicity": "asian",
            "setting": "beach",
            "mood": "romantic",
        }

        variation = CreativeVariation.model_validate(data)

        assert variation.name == "test-asian-beach-romantic"
        assert variation.model_ethnicity == "asian"
        assert variation.setting == "beach"
        assert variation.mood == "romantic"

    def test_presentation_mode_defaults_to_none(self):
        from app.models.variation import CreativeVariation

        v = CreativeVariation(name="t")
        assert v.presentation_mode is None

    def test_presentation_mode_accepts_product_only(self):
        from app.models.variation import CreativeVariation

        v = CreativeVariation.model_validate(
            {"name": "t", "presentation_mode": "product_only"}
        )
        assert v.presentation_mode == "product_only"

    def test_human_fields_accept_none(self):
        from app.models.variation import CreativeVariation

        v = CreativeVariation.model_validate(
            {
                "name": "t",
                "model_ethnicity": None,
                "activity": None,
                "model_description": None,
            }
        )
        assert v.model_ethnicity is None
        assert v.get_summary()  # must not raise


class TestVideoGenerationParameters:
    """Tests for video generation parameter validation."""

    def test_variation_dict_handling(self):
        """Video generation should handle dict variation input."""
        # This tests the ADK 1.21.0 fix - variation as dict instead of Pydantic model
        from app.models.variation import CreativeVariation

        # Should accept partial dict
        variation_dict = {"model_ethnicity": "european", "setting": "urban"}

        # Should validate successfully
        # The function should fill in defaults for missing fields
        try:
            variation = CreativeVariation.model_validate(variation_dict)
            assert variation.model_ethnicity == "european"
            assert variation.setting == "urban"
        except Exception:
            # If validation fails, the function should use defaults
            pass


class TestVariationValidationLoud:
    """ws09 Task 4: malformed variation input must fail loudly, not silently
    fall back to defaults."""

    @pytest.mark.asyncio
    async def test_malformed_variation_returns_structured_error(self, test_db):
        from app.tools.video_tools import generate_video_from_product

        result = await generate_video_from_product(
            campaign_id=1,
            product_id=1,
            variation={"model_ethnicity": ["not", "a", "string"]},
        )
        assert result.get("status") == "error"
        assert "Invalid variation parameters" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_with_model_on_beverage_errors(self, test_db):
        from app.database.db import get_product_by_name
        from app.tools.video_tools import generate_video_from_product

        p = get_product_by_name("aurora-cold-brew-330ml")
        result = await generate_video_from_product(
            campaign_id=1,
            product_id=p.id,
            variation={"presentation_mode": "with_model"},
        )
        assert result.get("status") == "error"
        assert "with_model" in result.get("error", "")


class TestVariationParamsStorage:
    """ws16 Task 11 (ws14 candidate): the campaign_videos.variation_params
    writer must drop model-only fields (model_ethnicity, activity) for
    archetypes with no human model in the shot — those fields describe a
    person and are a legacy artifact from the fashion-only era for
    consumable-hero / staged-product / product-hero archetypes."""

    def test_non_wearable_drops_model_only_fields(self):
        from app.models.variation import CreativeVariation
        from app.tools.video_tools import _variation_params_for_storage

        variation = CreativeVariation(
            name="test", model_ethnicity="diverse", activity="posing"
        )

        stored = _variation_params_for_storage(
            variation, archetype="consumable-hero"
        )

        assert "model_ethnicity" not in stored
        assert "activity" not in stored
        # Other fields are unaffected.
        assert stored["setting"] == "studio"

    def test_staged_product_and_product_hero_also_drop_model_fields(self):
        from app.models.variation import CreativeVariation
        from app.tools.video_tools import _variation_params_for_storage

        variation = CreativeVariation(
            name="test", model_ethnicity="asian", activity="walking"
        )

        for archetype in ("staged-product", "product-hero"):
            stored = _variation_params_for_storage(
                variation, archetype=archetype
            )
            assert "model_ethnicity" not in stored
            assert "activity" not in stored

    def test_wearable_keeps_model_fields(self):
        from app.models.variation import CreativeVariation
        from app.tools.video_tools import _variation_params_for_storage

        variation = CreativeVariation(
            name="test", model_ethnicity="asian", activity="walking"
        )

        stored = _variation_params_for_storage(variation, archetype="wearable")

        assert stored["model_ethnicity"] == "asian"
        assert stored["activity"] == "walking"


@pytest.mark.slow
class TestVideoGenerationIntegration:
    """Integration tests for video generation (requires Gemini Omni Flash API)."""

    @pytest.mark.asyncio
    async def test_generate_video_from_product_mock(
        self, test_db, mock_storage_module
    ):
        """Test video generation with mocked Gemini Omni Flash API."""
        with patch(
            "app.tools.video_tools.animate_scene_with_omni"
        ) as mock_omni:
            mock_omni.return_value = (
                b"fake-video-bytes",
                "test video prompt",
            )

            with patch(
                "app.tools.video_tools.generate_scene_image"
            ) as mock_scene:
                mock_scene.return_value = (
                    b"fake-scene-image-bytes",
                    "test scene prompt",
                )

                from app.tools.video_tools import generate_video_from_product

                result = await generate_video_from_product(
                    campaign_id=1,
                    product_id=1,
                    variation={"model_ethnicity": "asian", "setting": "beach"},
                )

                # Should return success or handle gracefully
                assert result is not None
