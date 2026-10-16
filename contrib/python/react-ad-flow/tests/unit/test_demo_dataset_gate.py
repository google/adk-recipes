"""Phase 15: DEMO_DATASET=fashion|none gates ALL demo seeding — including the
product seeding that used to live inside init_database() (db.py:259-261)."""

import importlib

import pytest

from tests._config_baseline import restore_config_baseline


class TestConfigParsing:
    def test_default_is_fashion(self):
        from app.config import DEMO_DATASET, DemoDataset

        assert DEMO_DATASET is DemoDataset.FASHION

    def test_invalid_value_raises_at_load(self, monkeypatch):
        monkeypatch.setenv("DEMO_DATASET", "bogus")
        import app.config

        with pytest.raises(ValueError, match="Invalid DEMO_DATASET"):
            importlib.reload(app.config)
        monkeypatch.delenv("DEMO_DATASET")
        restore_config_baseline()  # not reload: reload would re-derive under pinned session env


class TestGate:
    def test_init_database_no_longer_seeds(self, empty_test_db):
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM products")
            assert cursor.fetchone()[0] == 0

    def test_none_yields_empty_catalog(self, empty_test_db, monkeypatch):
        from app.config import DemoDataset

        monkeypatch.setattr("app.config.DEMO_DATASET", DemoDataset.NONE)
        from app.database.db import get_db_cursor
        from app.database.mock_data import seed_demo_data

        result = seed_demo_data()
        assert result["seeded"] is False
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM products")
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT COUNT(*) FROM campaigns")
            assert cursor.fetchone()[0] == 0

    def test_fashion_default_seeds_full_demo(self, empty_test_db):
        from app.database.db import get_db_cursor
        from app.database.mock_data import seed_demo_data

        result = seed_demo_data()
        assert result["seeded"] is True
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM products")
            assert cursor.fetchone()[0] == 28  # 22 fashion + 6 retail core
            cursor.execute("SELECT COUNT(*) FROM campaigns")
            assert cursor.fetchone()[0] == 4
        # Idempotent on re-run
        seed_demo_data()
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM products")
            assert cursor.fetchone()[0] == 28
