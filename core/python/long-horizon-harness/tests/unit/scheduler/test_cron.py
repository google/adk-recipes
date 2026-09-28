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

from datetime import UTC, datetime

import pytest

from app.scheduler.cron import is_valid_cron, next_cron_fire


def test_next_cron_fire_daily_8am():
    after = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)  # past 08:00
    nxt = next_cron_fire("0 8 * * *", after)
    assert nxt == datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


def test_next_cron_fire_is_strictly_after():
    after = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)  # exactly 08:00
    nxt = next_cron_fire("0 8 * * *", after)
    assert nxt > after


def test_is_valid_cron():
    assert is_valid_cron("0 8 * * *") is True
    assert is_valid_cron("*/15 * * * *") is True
    assert is_valid_cron("not a cron") is False
    assert is_valid_cron("") is False


def test_next_cron_fire_rejects_bad_expr():
    with pytest.raises(ValueError):
        next_cron_fire("nonsense", datetime(2026, 6, 14, tzinfo=UTC))
