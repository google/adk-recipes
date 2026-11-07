"""Phase 15: local-first storage — product-image local branches + URL policy.

Local mode = GCS_BUCKET None. conftest pins GCS_BUCKET=test-bucket session-wide,
so every local-mode test monkeypatches app.config.GCS_BUCKET to None (storage
functions read config at call time via function-level imports).
"""

from unittest.mock import MagicMock

import pytest

from app import storage


@pytest.fixture
def local_mode(tmp_path, monkeypatch):
    """Force local storage mode with a temp product-images root."""
    monkeypatch.setattr("app.config.GCS_BUCKET", None)
    monkeypatch.setattr(
        "app.config.PRODUCT_IMAGES_DIR", str(tmp_path / "product-images")
    )
    return tmp_path


@pytest.fixture
def gcs_mode(monkeypatch):
    """Force GCS mode with a mocked bucket (no real client construction)."""
    monkeypatch.setattr("app.config.GCS_BUCKET", "test-bucket")
    bucket = MagicMock()
    monkeypatch.setattr("app.storage._get_bucket", lambda: bucket)
    return bucket


class TestLocalProductImages:
    def test_save_read_exists_path_roundtrip(self, local_mode):
        path = storage.save_product_image("widget.png", b"\x89PNG-fake-bytes")
        assert path.startswith(str(local_mode))
        assert storage.product_image_exists("widget.png") is True
        assert storage.read_product_image("widget.png") == b"\x89PNG-fake-bytes"
        assert storage.get_product_image_path("widget.png") == path

    def test_exists_false_when_missing(self, local_mode):
        assert storage.product_image_exists("nope.png") is False

    def test_no_gcs_client_touched_in_local_mode(self, local_mode, monkeypatch):
        def boom():
            raise AssertionError("GCS client constructed in local mode")

        monkeypatch.setattr("app.storage._get_bucket", boom)
        storage.save_product_image("a.png", b"x")
        storage.product_image_exists("a.png")
        storage.read_product_image("a.png")
        storage.get_product_image_path("a.png")

    def test_public_urls_none_in_local_mode(self, local_mode):
        storage.save_product_image("here.png", b"x")
        assert storage.get_public_url("product-images/here.png") is None
        assert storage.get_product_image_public_url("here.png") is None
        assert storage.get_thumbnail_public_url("thumb.png") is None


class TestGcsProductImages:
    def test_public_url_none_when_blob_missing(self, gcs_mode):
        gcs_mode.blob.return_value.exists.return_value = False
        assert storage.get_product_image_public_url("gone.png") is None

    def test_public_url_when_blob_exists(self, gcs_mode):
        gcs_mode.blob.return_value.exists.return_value = True
        url = storage.get_product_image_public_url("real.png")
        assert (
            url
            == "https://storage.googleapis.com/test-bucket/product-images/real.png"
        )

    def test_save_content_type_by_extension(self, gcs_mode):
        storage.save_product_image("photo.jpg", b"jpegbytes")
        _, kwargs = gcs_mode.blob.return_value.upload_from_file.call_args
        assert kwargs["content_type"] == "image/jpeg"
        storage.save_product_image("photo.png", b"pngbytes")
        _, kwargs = gcs_mode.blob.return_value.upload_from_file.call_args
        assert kwargs["content_type"] == "image/png"


class TestConfigOptIn:
    def test_default_bucket_deleted(self):
        from app import config

        assert not hasattr(config, "DEFAULT_GCS_BUCKET")

    def test_product_images_dir_under_local_assets(self):
        from app import config

        assert config.PRODUCT_IMAGES_DIR.startswith(config.LOCAL_ASSETS_DIR)
        assert config.PRODUCT_IMAGES_DIR.endswith("product-images")
