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

"""A2UI v0.9 context and answer card surfaces for the web stage."""

from __future__ import annotations

from typing import Any

from app.context_cards import (
    A2UI_CATALOG_ID,
    A2UI_VERSION,
    active_context_cards,
)

__all__ = [
    "A2UI_CATALOG_ID",
    "A2UI_VERSION",
    "build_a2ui_surfaces",
]


def build_a2ui_surfaces() -> list[dict[str, Any]]:
    return list(active_context_cards())
