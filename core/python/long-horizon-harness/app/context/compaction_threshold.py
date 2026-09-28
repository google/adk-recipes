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

"""Percentage-of-window compaction threshold.

The fixed 750k token threshold mismatches any model whose window is not ~1M.
This derives the trigger from the active model's input limit so switching
models via ``/model`` keeps compaction firing at the same *fraction* of the
window. ADK reads ``EventsCompactionConfig.token_threshold`` live each turn, so
``select_model_callback`` recomputes and assigns this per turn.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_FRACTION = 0.75
WINDOW_FRACTION_ENV = "LHA_COMPACTION_WINDOW_FRACTION"


def _window_fraction() -> float:
    raw = os.environ.get(WINDOW_FRACTION_ENV)
    if raw is None:
        return DEFAULT_WINDOW_FRACTION
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using default", WINDOW_FRACTION_ENV, raw
        )
        return DEFAULT_WINDOW_FRACTION
    if not 0.0 < value < 1.0:
        logger.warning(
            "%s=%r out of (0,1); using default", WINDOW_FRACTION_ENV, raw
        )
        return DEFAULT_WINDOW_FRACTION
    return value


def compaction_token_threshold(input_token_limit: int) -> int:
    threshold = int(input_token_limit * _window_fraction())
    return max(1, threshold)
