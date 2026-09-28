# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest


def test_build_from_env_requires_dsn(monkeypatch):
    monkeypatch.delenv("LHA_REMINDER_DB_URL", raising=False)
    from app.scheduler.routine_postgres_store import build_from_env

    with pytest.raises(ValueError):
        build_from_env()


def test_row_to_routine_maps_fields():
    from datetime import UTC, datetime

    from app.scheduler.routine_postgres_store import _row_to_routine

    rec = {
        "id": "digest",
        "user_id": "alice@x",
        "app_name": "app",
        "schedule": "0 8 * * *",
        "task": "summarize",
        "secrets": '["STRIPE_KEY"]',
        "delivery": "report_back",
        "next_fire_at": datetime(2026, 6, 14, 8, 0, tzinfo=UTC),
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
    }
    row = _row_to_routine(rec)
    assert row.id == "digest"
    assert row.secrets == ("STRIPE_KEY",)
