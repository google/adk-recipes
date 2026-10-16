"""Unit tests for media-model configuration (Phase 1: model currency fix)."""

import importlib

import pytest

import app.config as config_module
from tests._config_baseline import restore_config_baseline


@pytest.fixture(autouse=True)
def _reload_config_after_test(monkeypatch):
    """Each test may reload app.config; restore the import-time baseline afterwards."""
    yield
    monkeypatch.undo()
    restore_config_baseline()


def test_media_model_defaults_are_ga_ids(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    monkeypatch.delenv("VIDEO_GEN_MODEL", raising=False)
    cfg = importlib.reload(config_module)
    assert cfg.MODEL == "gemini-3.7-flash"
    assert cfg.IMAGE_GENERATION == "gemini-3-pro-image"
    assert cfg.VIDEO_GEN_MODEL == "gemini-omni-flash-preview"


def test_media_models_are_env_overridable(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "fake-agent-model-id")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "fake-image-preview-id")
    monkeypatch.setenv("VIDEO_GEN_MODEL", "fake-video-preview-id")
    cfg = importlib.reload(config_module)
    assert cfg.MODEL == "fake-agent-model-id"
    assert cfg.IMAGE_GENERATION == "fake-image-preview-id"
    assert cfg.VIDEO_GEN_MODEL == "fake-video-preview-id"


def test_old_veo_model_name_is_gone():
    cfg = importlib.reload(config_module)
    assert not hasattr(cfg, "VEO_MODEL")


class TestCampaignCategoriesParity:
    """config.CAMPAIGN_CATEGORIES must stay in sync with the CHECK
    constraint on campaigns.category (app/database/db.py — source of truth)."""

    def test_all_config_categories_accepted_by_db(self, test_db):
        from app import config
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            for cat in config.CAMPAIGN_CATEGORIES:
                # Raises sqlite3.IntegrityError if cat violates the CHECK
                cursor.execute(
                    "INSERT INTO campaigns (name, city, state, category) "
                    "VALUES (?, ?, ?, ?)",
                    (f"parity-{cat}", "Los Angeles", "CA", cat),
                )

    def test_holiday_category_present(self):
        from app import config

        assert "holiday" in config.CAMPAIGN_CATEGORIES


class TestAppMode:
    """Phase 6: typed APP_MODE config value (demo|connected), no consumers yet."""

    def test_unset_defaults_to_demo(self, monkeypatch):
        monkeypatch.delenv("APP_MODE", raising=False)
        cfg = importlib.reload(config_module)
        assert cfg.APP_MODE is cfg.AppMode.DEMO

    def test_explicit_demo_is_demo(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "demo")
        cfg = importlib.reload(config_module)
        assert cfg.APP_MODE is cfg.AppMode.DEMO

    def test_connected_is_valid_and_readable(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "connected")
        cfg = importlib.reload(config_module)
        assert cfg.APP_MODE is cfg.AppMode.CONNECTED

    def test_value_is_normalized(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "  Connected ")
        cfg = importlib.reload(config_module)
        assert cfg.APP_MODE is cfg.AppMode.CONNECTED

    def test_empty_value_means_unset(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "")
        cfg = importlib.reload(config_module)
        assert cfg.APP_MODE is cfg.AppMode.DEMO

    def test_invalid_value_raises_valueerror_at_load(self, monkeypatch):
        monkeypatch.setenv("APP_MODE", "garbage")
        with pytest.raises(ValueError, match="APP_MODE"):
            importlib.reload(config_module)

    def test_app_mode_is_str_enum(self, monkeypatch):
        monkeypatch.delenv("APP_MODE", raising=False)
        cfg = importlib.reload(config_module)
        assert isinstance(cfg.APP_MODE, cfg.AppMode)
        assert (
            cfg.APP_MODE == "demo"
        )  # str-enum: comparable to its string value


class TestBlueZooValidPolicy:
    """Phase 11b: Rule R as explicit, configurable policy — never baked in."""

    def test_unset_defaults_to_valid_only(self, monkeypatch):
        monkeypatch.delenv("BLUEZOO_VALID_POLICY", raising=False)
        cfg = importlib.reload(config_module)
        assert cfg.BLUEZOO_VALID_POLICY is cfg.BlueZooValidPolicy.VALID_ONLY

    def test_include_all_is_valid(self, monkeypatch):
        monkeypatch.setenv("BLUEZOO_VALID_POLICY", "include-all")
        cfg = importlib.reload(config_module)
        assert cfg.BLUEZOO_VALID_POLICY is cfg.BlueZooValidPolicy.INCLUDE_ALL

    def test_value_is_normalized(self, monkeypatch):
        monkeypatch.setenv("BLUEZOO_VALID_POLICY", "  Valid-Only ")
        cfg = importlib.reload(config_module)
        assert cfg.BLUEZOO_VALID_POLICY is cfg.BlueZooValidPolicy.VALID_ONLY

    def test_empty_value_means_unset(self, monkeypatch):
        monkeypatch.setenv("BLUEZOO_VALID_POLICY", "")
        cfg = importlib.reload(config_module)
        assert cfg.BLUEZOO_VALID_POLICY is cfg.BlueZooValidPolicy.VALID_ONLY

    def test_invalid_value_raises_valueerror_at_load(self, monkeypatch):
        monkeypatch.setenv("BLUEZOO_VALID_POLICY", "garbage")
        with pytest.raises(ValueError, match="BLUEZOO_VALID_POLICY"):
            importlib.reload(config_module)


class TestOmniEditConfig:
    def test_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv("ENABLE_OMNI_EDIT", raising=False)
        monkeypatch.delenv("OMNI_EDIT_MODEL", raising=False)
        cfg = importlib.reload(config_module)
        assert cfg.ENABLE_OMNI_EDIT is False
        assert cfg.OMNI_EDIT_MODEL == "gemini-omni-flash-preview"

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("ENABLE_OMNI_EDIT", "true")
        cfg = importlib.reload(config_module)
        assert cfg.ENABLE_OMNI_EDIT is True
