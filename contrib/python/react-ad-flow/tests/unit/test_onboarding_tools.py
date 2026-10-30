"""Phase 15 onboarding tools: create_product / import_products_from_folder /
generate_product_image — all against the empty-catalog fixture, local mode."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.GCS_BUCKET", None)
    monkeypatch.setattr(
        "app.config.PRODUCT_IMAGES_DIR", str(tmp_path / "product-images")
    )
    return tmp_path


class TestCreateProduct:
    def test_create_success_pending_image(self, empty_test_db, local_mode):
        from app.tools.onboarding_tools import create_product

        result = create_product(
            name="Aurora Cold Brew 330ml",
            category="beverage",
            description="Nitro cold brew in a slim can",
            attributes={"volume_ml": 330, "caffeine_mg": 120},
        )
        assert result["status"] == "success"
        assert result["product"]["id"] is not None
        assert (
            result["product"]["image_filename"] == "aurora-cold-brew-330ml.png"
        )
        assert result["product"]["image_status"] == "pending"
        assert "next_steps" in result

    def test_attributes_roundtrip(self, empty_test_db, local_mode):
        from app.database.db import get_product_by_name
        from app.tools.onboarding_tools import create_product

        create_product(
            name="Trail Shoe X",
            category="footwear",
            attributes={"waterproof": True, "weight_grams": 240},
        )
        stored = get_product_by_name("Trail Shoe X")
        assert stored.attributes["weight_grams"] == 240

    def test_duplicate_name_translated(self, empty_test_db, local_mode):
        from app.tools.onboarding_tools import create_product

        create_product(name="Dup Widget", category="electronics")
        result = create_product(name="Dup Widget", category="electronics")
        assert result["status"] == "error"
        assert "already exists" in result["message"]

    def test_empty_name_rejected(self, empty_test_db, local_mode):
        from app.tools.onboarding_tools import create_product

        assert create_product(name="   ")["status"] == "error"


class TestImportFromFolder:
    def _folder(self, tmp_path):
        src = tmp_path / "vendor-images"
        src.mkdir()
        (src / "Red Mug.png").write_bytes(b"\x89PNG" + b"x" * 20000)
        (src / "blue-bottle.jpg").write_bytes(b"\xff\xd8" + b"y" * 20000)
        (src / "tiny-icon.png").write_bytes(
            b"\x89PNG" + b"z" * 100
        )  # tiny → warn
        (src / "notes.txt").write_text("not an image")
        return src

    def test_import_creates_products_and_saves_images(
        self, empty_test_db, local_mode, tmp_path
    ):
        from app.tools.onboarding_tools import import_products_from_folder

        result = import_products_from_folder(
            str(self._folder(tmp_path)), category="homeware"
        )
        assert result["status"] == "success"
        assert len(result["created"]) == 3  # txt ignored
        assert len(result["warnings"]) == 1  # tiny image warned, not blocked
        names = {p["name"] for p in result["created"]}
        assert names == {"red-mug", "blue-bottle", "tiny-icon"}
        from app import storage

        assert storage.product_image_exists("red-mug.png")
        assert storage.product_image_exists("blue-bottle.jpg")

    def test_reimport_skips_duplicates(
        self, empty_test_db, local_mode, tmp_path
    ):
        from app.tools.onboarding_tools import import_products_from_folder

        folder = str(self._folder(tmp_path))
        import_products_from_folder(folder)
        result = import_products_from_folder(folder)
        assert len(result["created"]) == 0
        assert len(result["skipped"]) == 3

    def test_missing_folder(self, empty_test_db, local_mode):
        from app.tools.onboarding_tools import import_products_from_folder

        assert import_products_from_folder("/no/such/dir")["status"] == "error"


class TestGenerateProductImage:
    def _mock_genai(self, monkeypatch, image_bytes=b"generated-png-bytes"):
        part = SimpleNamespace(inline_data=SimpleNamespace(data=image_bytes))
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
        )
        client = MagicMock()
        client.models.generate_content.return_value = response
        monkeypatch.setattr(
            "app.tools.onboarding_tools.genai.Client", lambda: client
        )
        return client

    def test_generates_and_stores(self, empty_test_db, local_mode, monkeypatch):
        from app.database.db import get_product
        from app.tools.onboarding_tools import (
            create_product,
            generate_product_image,
        )

        client = self._mock_genai(monkeypatch)
        created = create_product(
            name="Aurora Cold Brew 330ml", category="beverage"
        )
        product_id = created["product"]["id"]
        result = asyncio.run(generate_product_image(product_id=product_id))
        assert result["status"] == "success"
        assert result["product"]["image_status"] == "available"
        from app import storage

        assert storage.product_image_exists("aurora-cold-brew-330ml.png")
        assert get_product(product_id).local_path is not None
        prompt = client.models.generate_content.call_args.kwargs["contents"][0]
        assert "beverage" in prompt  # category-aware prompt

    def test_by_name_and_style_hint(
        self, empty_test_db, local_mode, monkeypatch
    ):
        from app.tools.onboarding_tools import (
            create_product,
            generate_product_image,
        )

        client = self._mock_genai(monkeypatch)
        create_product(name="Trail Shoe X", category="footwear")
        result = asyncio.run(
            generate_product_image(
                product_name="Trail Shoe X", style_hint="warm morning light"
            )
        )
        assert result["status"] == "success"
        prompt = client.models.generate_content.call_args.kwargs["contents"][0]
        assert "warm morning light" in prompt

    def test_unknown_product(self, empty_test_db, local_mode):
        from app.tools.onboarding_tools import generate_product_image

        result = asyncio.run(generate_product_image(product_id=999))
        assert result["status"] == "error"

    def test_no_image_in_response(self, empty_test_db, local_mode, monkeypatch):
        from app.tools.onboarding_tools import (
            create_product,
            generate_product_image,
        )

        part = SimpleNamespace(inline_data=None)
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
        )
        client = MagicMock()
        client.models.generate_content.return_value = response
        monkeypatch.setattr(
            "app.tools.onboarding_tools.genai.Client", lambda: client
        )
        created = create_product(name="Ghost Product", category="misc")
        result = asyncio.run(
            generate_product_image(product_id=created["product"]["id"])
        )
        assert result["status"] == "error"


class TestAgentWiring:
    def test_campaign_agent_has_onboarding_tools(self):
        from app.agent import campaign_agent
        from app.tools.onboarding_tools import (
            create_product,
            generate_product_image,
            import_products_from_folder,
        )

        assert create_product in campaign_agent.tools
        assert import_products_from_folder in campaign_agent.tools
        assert generate_product_image in campaign_agent.tools
