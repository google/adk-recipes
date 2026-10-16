"""Typed-adapter regression tests: get_product/list_products return Product
and the migration does not change video animation prompt text (Phase 8)."""

from pathlib import Path

from app.database.db import get_product, get_product_by_name, list_products
from app.models.product import Product
from app.models.variation import CreativeVariation
from app.tools.prompt_builders import (
    build_creative_prompt,
    build_scene_image_prompt,
)

GOLDEN_DIR = Path(__file__).parent / "data"


class TestTypedReturns:
    def test_get_product_returns_typed(self, test_db):
        p = get_product(1)
        assert isinstance(p, Product)
        assert p.id == 1
        assert p.name == "black-high-waist-trousers"
        assert p.attributes["style"] == "tailored wide-leg trousers"

    def test_get_product_missing_returns_none(self, test_db):
        assert get_product(999999) is None

    def test_get_product_by_name_typed(self, test_db):
        p = get_product_by_name("black-high-waist-trousers")
        assert isinstance(p, Product)
        assert p.id == 1

    def test_all_seed_products_typed_and_valid(self, test_db):
        products = list_products()
        assert len(products) >= 22
        for p in products:
            assert isinstance(p, Product)
            assert p.id and p.name and p.image_filename

    def test_category_filter_typed(self, test_db):
        for p in list_products(category="dress"):
            assert p.category == "dress"

    def test_pattern_metadata_only_key_present(self, test_db):
        # 7/22 seed entries have 'pattern' only in metadata JSON
        assert sum(1 for p in list_products() if "pattern" in p.attributes) >= 1


class TestGoldenPrompts:
    """The adapter migration must not change prompt text for the fashion
    catalog (style -> category -> literal fallback preserved)."""

    def test_scene_prompt_unchanged(self, test_db):
        product = get_product(1)
        variation = CreativeVariation(
            name="golden-baseline-asian", model_ethnicity="asian"
        )
        expected = (
            GOLDEN_DIR / "golden_scene_prompt_product1_asian.txt"
        ).read_text()
        assert build_scene_image_prompt(product, variation) == expected

    def test_creative_prompt_unchanged(self, test_db):
        product = get_product(1)
        variation = CreativeVariation(
            name="golden-baseline-asian", model_ethnicity="asian"
        )
        expected = (
            GOLDEN_DIR / "golden_creative_prompt_product1_asian.txt"
        ).read_text()
        assert build_creative_prompt(product, variation) == expected

    def test_null_style_beverage_routes_to_product_prompt(self):
        """ws08 pinned the fashion fallback for style-less products; ws09 replaces it:
        a beverage now gets a product-centric prompt, never a garment prompt."""
        product = Product(
            name="test-beverage",
            category="beverage",
            description="A refreshing drink",
            image_filename="b.png",
            attributes={},
        )
        prompt = build_scene_image_prompt(product, CreativeVariation(name="t"))
        assert "wearing" not in prompt.lower()
        assert "Test Beverage" in prompt
        assert "None" not in prompt


class TestToolOutputContractsUnchanged:
    def test_list_products_tool_keys(self, test_db):
        from app.tools.video_tools import list_products as list_products_tool

        result = list_products_tool(include_urls=False)
        assert result["status"] == "success"
        product = result["products"][0]
        for key in (
            "id",
            "name",
            "category",
            "style",
            "color",
            "fabric",
            "image_filename",
        ):
            assert key in product

    def test_create_campaign_still_works_typed(self, test_db):
        from app.tools.campaign_tools import create_campaign

        result = create_campaign(
            product_id=1,
            store_name="Adapter Test Store",
            city="Austin",
            state="TX",
        )
        assert result["status"] == "success"
        assert result["campaign"]["product_name"] == "black-high-waist-trousers"
