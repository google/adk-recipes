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

"""Environment-variable parsing helpers."""

from __future__ import annotations

import os

_TRUTHY = ("1", "true", "yes")


def env_flag(name: str, *, default: bool = False) -> bool:
    """True iff ``$name`` is one of ``1``/``true``/``yes`` (case-insensitive).

    Only an absent var falls back to ``default``; an empty value is falsy.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def env_str(name: str, default: str = "") -> str:
    """``$name`` stripped, or ``default`` when unset OR empty.

    Unlike ``os.environ.get(name, default)``, a blank value falls back, so a
    name-only entry in ``.env.example`` cannot override a code default.
    """
    return os.environ.get(name, "").strip() or default
