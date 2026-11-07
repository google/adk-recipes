"""Tests for the generic Product model and its row adapter (Phase 8)."""

import json

from app.models.product import Product


def _fashion_row(**overrides):
    """A row shaped like the seeded fashion catalog (both seeders dump the
    full entry dict into metadata, duplicating the typed columns)."""
    entry = {
        "name": "black-high-waist-trousers",
        "category": "pants",
        "style": "tailored wide-leg trousers",
        "color": "classic black",
        "fabric": "wool blend suiting",
        "details": "high waist, pleated front, wide straight leg, side pockets",
        "occasion": "office and professional wear",
        "image_filename": "black-high-waist-trousers.png",
        "local_path": "scripts/products/black-high-waist-trousers.png",
    }
    row = {
        "id": 1,
        **{
            k: entry.get(k)
            for k in (
                "name",
                "category",
                "style",
                "color",
                "fabric",
                "details",
                "occasion",
                "image_filename",
                "local_path",
            )
        },
        "gcs_path": None,
        "metadata": json.dumps(entry),
        "created_at": "2026-07-20 00:00:00",
    }
    row.update(overrides)
    return row


class TestFromRow:
    def test_core_fields(self):
        p = Product.from_row(_fashion_row())
        assert p.id == 1
        assert p.name == "black-high-waist-trousers"
        assert p.category == "pants"
        assert (
            p.description
            == "high waist, pleated front, wide straight leg, side pockets"
        )
        assert p.image_filename == "black-high-waist-trousers.png"

    def test_fashion_columns_fold_into_attributes(self):
        p = Product.from_row(_fashion_row())
        assert p.attributes["style"] == "tailored wide-leg trousers"
        assert p.attributes["color"] == "classic black"
        assert p.attributes["fabric"] == "wool blend suiting"
        assert p.attributes["occasion"] == "office and professional wear"

    def test_attributes_exclude_core_column_duplicates(self):
        p = Product.from_row(_fashion_row())
        for core in (
            "name",
            "category",
            "details",
            "image_filename",
            "local_path",
        ):
            assert core not in p.attributes

    def test_metadata_only_key_survives(self):
        # 7 of 22 seeded products carry 'pattern' ONLY inside metadata JSON
        entry_meta = json.loads(_fashion_row()["metadata"])
        entry_meta["pattern"] = "pinstripe"
        p = Product.from_row(_fashion_row(metadata=json.dumps(entry_meta)))
        assert p.attributes["pattern"] == "pinstripe"

    def test_row_column_wins_over_metadata(self):
        row = _fashion_row(style="updated style")
        p = Product.from_row(row)
        assert p.attributes["style"] == "updated style"

    def test_null_and_invalid_metadata_tolerated(self):
        assert (
            Product.from_row(_fashion_row(metadata=None)).attributes["style"]
            == "tailored wide-leg trousers"
        )
        assert (
            Product.from_row(_fashion_row(metadata="{not json")).attributes[
                "color"
            ]
            == "classic black"
        )

    def test_attributes_first_row_no_fashion_columns(self):
        row = {
            "id": 30,
            "name": "aurora-cold-brew-330ml",
            "category": "beverage",
            "style": None,
            "color": None,
            "fabric": None,
            "occasion": None,
            "details": "slow-steeped single-origin cold brew",
            "image_filename": "aurora-cold-brew-330ml.png",
            "gcs_path": None,
            "local_path": None,
            "created_at": None,
            "metadata": json.dumps(
                {"flavor": "dark chocolate notes", "volume_ml": 330}
            ),
        }
        p = Product.from_row(row)
        assert p.attributes == {
            "flavor": "dark chocolate notes",
            "volume_ml": 330,
        }
        assert "style" not in p.attributes


class TestToRowRoundTrip:
    def test_round_trip_is_lossless(self):
        original = Product(
            name="aurora-cold-brew-330ml",
            category="beverage",
            description="slow-steeped single-origin cold brew",
            image_filename="aurora-cold-brew-330ml.png",
            attributes={
                "flavor": "dark chocolate notes",
                "volume_ml": 330,
                "packaging": "slim can",
                "dietary_tags": ["vegan"],
            },
        )
        row = original.to_row()
        row.update({"id": 99, "created_at": None})
        restored = Product.from_row(row)
        assert restored.name == original.name
        assert restored.category == original.category
        assert restored.description == original.description
        assert restored.attributes == original.attributes

    def test_to_row_puts_legacy_attributes_in_typed_columns(self):
        p = Product.from_row(_fashion_row())
        row = p.to_row()
        assert row["style"] == "tailored wide-leg trousers"
        assert row["color"] == "classic black"
        assert row["details"] == p.description
        assert json.loads(row["metadata"])["fabric"] == "wool blend suiting"
