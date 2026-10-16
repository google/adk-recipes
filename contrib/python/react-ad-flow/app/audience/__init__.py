"""Audience data-source selection (Phase 11a).

APP_MODE is the only mode knob (Phase 6): demo → SyntheticAudienceDataSource;
connected → LiveBlueZooAudienceDataSource (Phase 11b), which fails closed at
construction if BlueZoo configuration is missing.

Ported from the donor factory's mechanics — lazy dotted-path registry,
thread-safe singleton, test-override seam — minus its traps: no GCS_BUCKET
coupling (the donor gated its BQ provider on an env var the provider never
used), no BigQuery-by-default, no reach-in to other services for seeding.
Entry-points packaging deliberately skipped (phase doc): the builtin dict
is the registry.
"""

import importlib
import threading

from ..config import AppMode
from .datasource import AudienceDataSource

__all__ = [
    "AudienceDataSource",
    "get_audience_datasource",
    "register_datasource_for_tests",
    "reset_audience_datasource",
]

_BUILTIN_SOURCES: dict[AppMode, str] = {
    AppMode.DEMO: "app.audience.synthetic:SyntheticAudienceDataSource",
    AppMode.CONNECTED: "app.audience.live_bluezoo:LiveBlueZooAudienceDataSource",
}

_lock = threading.Lock()
_singleton: AudienceDataSource | None = None
_test_override: AudienceDataSource | None = None


def get_audience_datasource() -> AudienceDataSource:
    """Process-wide audience source, resolved from config.APP_MODE."""
    if _test_override is not None:
        return _test_override
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = _instantiate_for_mode()
    return _singleton


def _instantiate_for_mode() -> AudienceDataSource:
    from .. import (
        config,
    )  # attribute read at call time: tests monkeypatch APP_MODE

    mode = config.APP_MODE
    dotted = _BUILTIN_SOURCES.get(mode)
    if dotted is None:
        # Defensive guard, not a Phase 11b gap: every AppMode member must
        # have a _BUILTIN_SOURCES entry, so reaching here is a bug.
        raise RuntimeError(
            f"No audience data source is registered for APP_MODE={mode.value!r} "
            "— this is a bug (every AppMode member must have a _BUILTIN_SOURCES "
            "entry). Silently falling back to demo data is not allowed."
        )
    module_name, _, class_name = dotted.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls()


def register_datasource_for_tests(source: AudienceDataSource) -> None:
    """Bypass mode resolution entirely — for tests only."""
    global _test_override
    _test_override = source


def reset_audience_datasource() -> None:
    """Clear singleton and test override (test isolation)."""
    global _singleton, _test_override
    with _lock:
        _singleton = None
        _test_override = None
