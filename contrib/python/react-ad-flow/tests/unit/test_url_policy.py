"""ws09 bar: tool responses never emit storage.googleapis.com in local mode,
and GCS-mode URLs are existence-checked (no dead links). Regression net over
list_products and the review tools; maps_tools shares the same storage
helpers and is exercised by the demo scenarios."""

import json

import pytest


@pytest.fixture
def local_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.GCS_BUCKET", None)
    monkeypatch.setattr(
        "app.config.PRODUCT_IMAGES_DIR", str(tmp_path / "product-images")
    )
    return tmp_path


class TestLocalModeNeverEmitsStorageUrls:
    def test_list_products_local(self, test_db, local_mode):
        from app.tools.video_tools import list_products

        result = list_products()
        assert result["status"] == "success"
        assert "storage.googleapis.com" not in json.dumps(result)
        for product in result["products"]:
            assert product["image_status"] in ("available", "missing")
            assert "image_url" not in product  # no public URLs in local mode

    def test_review_table_local(self, test_db, local_mode):
        from app.tools.review_tools import get_video_review_table

        result = get_video_review_table()
        assert "storage.googleapis.com" not in json.dumps(result)

    def test_video_details_local(self, test_db, local_mode):
        from app.tools.review_tools import (
            get_video_details,
            get_video_review_table,
        )

        table = get_video_review_table()
        video_id = table["videos"][0]["id"]
        result = get_video_details(video_id)
        assert "storage.googleapis.com" not in json.dumps(result)


class TestGcsModeUrlsAreExistenceChecked:
    def test_missing_video_yields_no_url(self, test_db, monkeypatch):
        # conftest pins GCS_BUCKET=test-bucket → GCS mode; force "nothing exists"
        monkeypatch.setattr("app.storage.video_exists", lambda p: False)
        monkeypatch.setattr("app.storage.product_image_exists", lambda f: False)
        from app.tools.review_tools import get_video_review_table

        result = get_video_review_table()
        for video in result["videos"]:
            assert video.get("video_url") is None  # no dead-link fallback

    def test_list_products_missing_image_no_url(self, test_db, monkeypatch):
        monkeypatch.setattr("app.storage.product_image_exists", lambda f: False)
        from app.tools.video_tools import list_products

        result = list_products()
        for product in result["products"]:
            assert "image_url" not in product
            assert product["image_status"] == "missing"
