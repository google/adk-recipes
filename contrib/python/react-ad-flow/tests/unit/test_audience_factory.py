"""APP_MODE → audience-source selection; Phase 6's fail-closed guard, built."""

import pytest

import app.config
from app.audience import (
    get_audience_datasource,
    register_datasource_for_tests,
    reset_audience_datasource,
)
from app.audience.synthetic import SyntheticAudienceDataSource
from app.config import AppMode


@pytest.fixture(autouse=True)
def _reset():
    reset_audience_datasource()
    yield
    reset_audience_datasource()


class TestDemoMode:
    def test_demo_resolves_to_synthetic(self, monkeypatch):
        monkeypatch.setattr(app.config, "APP_MODE", AppMode.DEMO)
        assert isinstance(
            get_audience_datasource(), SyntheticAudienceDataSource
        )

    def test_singleton_same_instance_across_calls(self, monkeypatch):
        monkeypatch.setattr(app.config, "APP_MODE", AppMode.DEMO)
        assert get_audience_datasource() is get_audience_datasource()


class TestConnectedFailsClosed:
    """Connected mode still fails closed — now at conformer construction,
    naming the missing configuration instead of a missing implementation."""

    def _clear_bluezoo_env(self, monkeypatch):
        for var in (
            "BLUEZOO_BASE_URL",
            "BLUEZOO_ACCESS_KEY",
            "BLUEZOO_SENSOR_MAP",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_connected_without_config_raises_specific_error(self, monkeypatch):
        from app.audience.live_bluezoo import BlueZooConfigError

        monkeypatch.setattr(app.config, "APP_MODE", AppMode.CONNECTED)
        self._clear_bluezoo_env(monkeypatch)
        reset_audience_datasource()
        with pytest.raises(BlueZooConfigError, match="BLUEZOO_BASE_URL"):
            get_audience_datasource()

    def test_no_silent_fallback_to_demo(self, monkeypatch):
        monkeypatch.setattr(app.config, "APP_MODE", AppMode.CONNECTED)
        self._clear_bluezoo_env(monkeypatch)
        reset_audience_datasource()
        with pytest.raises(Exception):  # noqa: B017 — any failure is fine, the point is "no fallback"
            get_audience_datasource()
        # And the failure must not have cached a synthetic fallback:
        with pytest.raises(Exception):  # noqa: B017
            get_audience_datasource()

    def test_connected_with_full_env_resolves_to_live_source(self, monkeypatch):
        from app.audience.live_bluezoo import LiveBlueZooAudienceDataSource

        monkeypatch.setattr(app.config, "APP_MODE", AppMode.CONNECTED)
        monkeypatch.setenv("BLUEZOO_BASE_URL", "https://stub.invalid/v2/dwh")
        monkeypatch.setenv("BLUEZOO_ACCESS_KEY", "stub-key")
        monkeypatch.setenv("BLUEZOO_SENSOR_MAP", "101:87")
        reset_audience_datasource()
        assert isinstance(
            get_audience_datasource(), LiveBlueZooAudienceDataSource
        )
        reset_audience_datasource()

    def test_construction_failure_is_not_cached(self, monkeypatch):
        """Owner-named property: a failed _instantiate_for_mode() must leave
        _singleton None, so fixing the env works WITHOUT a reset."""
        from app.audience.live_bluezoo import (
            BlueZooConfigError,
            LiveBlueZooAudienceDataSource,
        )

        monkeypatch.setattr(app.config, "APP_MODE", AppMode.CONNECTED)
        self._clear_bluezoo_env(monkeypatch)
        reset_audience_datasource()
        with pytest.raises(BlueZooConfigError):
            get_audience_datasource()
        monkeypatch.setenv("BLUEZOO_BASE_URL", "https://stub.invalid/v2/dwh")
        monkeypatch.setenv("BLUEZOO_ACCESS_KEY", "stub-key")
        monkeypatch.setenv("BLUEZOO_SENSOR_MAP", "101:87")
        # Deliberately NO reset between the failure and this call:
        assert isinstance(
            get_audience_datasource(), LiveBlueZooAudienceDataSource
        )
        reset_audience_datasource()


class TestTestSeam:
    def test_override_bypasses_mode_resolution(self, monkeypatch):
        monkeypatch.setattr(app.config, "APP_MODE", AppMode.CONNECTED)

        class _Fake(SyntheticAudienceDataSource):
            pass

        fake = _Fake()
        register_datasource_for_tests(fake)
        assert get_audience_datasource() is fake  # even in connected mode

    def test_reset_clears_override_and_singleton(self, monkeypatch):
        monkeypatch.setattr(app.config, "APP_MODE", AppMode.DEMO)
        first = get_audience_datasource()
        register_datasource_for_tests(SyntheticAudienceDataSource())
        reset_audience_datasource()
        assert get_audience_datasource() is not first  # fresh singleton
