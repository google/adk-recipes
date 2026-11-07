"""The multi-vertical retail core test set proves the schema is
vertical-agnostic (Phase 8, owner decision: general retail, not one vertical)."""

import pytest

from app.database.db import get_product_by_name, populate_retail_test_products
from app.database.retail_products_data import RETAIL_TEST_PRODUCTS
from app.models.product import Product
from app.tools.campaign_tools import create_campaign

EXPECTED_VERTICALS = {
    "beverage",
    "qsr-menu-item",
    "electronics",
    "furniture",
    "home-appliance",
}


@pytest.fixture()
def retail_db(test_db):
    populate_retail_test_products()  # idempotent; test copy may predate fixtures
    return test_db


class TestCoreTestSet:
    def test_covers_all_five_verticals(self, retail_db):
        assert {
            e["category"] for e in RETAIL_TEST_PRODUCTS
        } == EXPECTED_VERTICALS

    def test_entries_are_attributes_first(self):
        for entry in RETAIL_TEST_PRODUCTS:
            assert entry["attributes"], f"{entry['name']} has no attributes"
            for fashion_key in ("style", "color", "fabric", "occasion"):
                assert fashion_key not in entry["attributes"], (
                    f"{entry['name']} uses fashion key '{fashion_key}'"
                )

    def test_every_fixture_retrievable_typed_with_attributes(self, retail_db):
        for entry in RETAIL_TEST_PRODUCTS:
            p = get_product_by_name(entry["name"])
            assert isinstance(p, Product), entry["name"]
            assert p.category == entry["category"]
            assert p.attributes == entry["attributes"]
            assert p.description == entry["description"]

    def test_populate_is_idempotent(self, retail_db):
        before = get_product_by_name(RETAIL_TEST_PRODUCTS[0]["name"]).id
        populate_retail_test_products()
        assert get_product_by_name(RETAIL_TEST_PRODUCTS[0]["name"]).id == before

    def test_beverage_campaign_gets_always_on(self, retail_db):
        p = get_product_by_name("aurora-cold-brew-330ml")
        result = create_campaign(
            product_id=p.id,
            store_name="Target Downtown",
            city="Austin",
            state="TX",
        )
        assert result["status"] == "success"
        assert result["campaign"]["category"] == "always-on"
        assert "fashion item" not in result["campaign"]["description"]
        assert "Aurora Cold Brew" in result["campaign"]["description"]


class TestSelfServiceOnTheFly:
    """Owner directive: a vendor's never-before-seen product must work end to
    end (persist -> typed retrieve -> campaign) with only an image REFERENCE —
    the file may not exist yet; upload/generation land in Phase 14a/15."""

    def test_vendor_product_end_to_end(self, retail_db):
        from app.database.db import insert_product

        vendor_product = Product(
            name="vendor-demo-trail-shoe",
            category="footwear",  # a vertical NOT in the core set
            description="lightweight waterproof trail running shoe",
            image_filename="vendor-demo-trail-shoe.png",  # not on disk anywhere
            attributes={
                "brand": "Summit Labs",
                "sizes": ["8", "9", "10"],
                "waterproof_rating": "IPX7",
                "weight_grams": 240,
            },
        )
        stored = insert_product(vendor_product)
        assert stored.id is not None
        fetched = get_product_by_name("vendor-demo-trail-shoe")
        assert fetched.attributes == vendor_product.attributes
        assert fetched.description == vendor_product.description
        result = create_campaign(
            product_id=stored.id,
            store_name="Vendor Demo Store",
            city="Austin",
            state="TX",
        )
        assert result["status"] == "success"
        assert result["campaign"]["category"] == "always-on"
        assert "fashion item" not in result["campaign"]["description"]

    def test_duplicate_name_raises_integrity_error(self, retail_db):
        import sqlite3

        from app.database.db import insert_product

        duplicate = Product(
            name="aurora-cold-brew-330ml",
            category="beverage",
            description="dup",
            image_filename="x.png",
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_product(duplicate)
