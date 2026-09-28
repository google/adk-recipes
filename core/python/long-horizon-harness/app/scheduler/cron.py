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

"""Cron-expression helpers for routine scheduling (5-field cron via croniter)."""

from __future__ import annotations

from datetime import datetime

from croniter import croniter


def is_valid_cron(schedule: str) -> bool:
    return bool(schedule) and croniter.is_valid(schedule)


def next_cron_fire(schedule: str, after: datetime) -> datetime:
    """Next fire time strictly after ``after`` for a 5-field cron ``schedule``.

    Raises ValueError on an invalid expression.
    """
    if not is_valid_cron(schedule):
        raise ValueError(f"invalid cron schedule: {schedule!r}")
    return croniter(schedule, after).get_next(datetime)
