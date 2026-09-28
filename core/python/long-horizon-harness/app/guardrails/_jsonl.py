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

"""Shared parser for the ``.lha/*.jsonl`` tenant overlays.

Each overlay is one JSON object per line; blank lines and ``#`` comments are
skipped, malformed lines are logged and dropped. Callers map the yielded dicts
to their own domain type.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

_logger = logging.getLogger(__name__)


def iter_jsonl_objects(text: str, *, context: str) -> Iterator[dict]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            _logger.warning("%s: skipping malformed JSONL line", context)
            continue
        if isinstance(obj, dict):
            yield obj
