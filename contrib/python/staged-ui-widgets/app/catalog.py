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
"""The recipe's only contact point with the A2UI SDK.

Every widget here is built from the *stock* v0.9 basic catalog -- Card,
Column, Row, List, Text, Image, Icon, Divider. No custom catalog is needed,
so there is no catalog JSON to ship and nothing to keep in sync with a
front end.

Resolving the catalog id through the SDK rather than hardcoding the string
means pointing the recipe at a richer catalog is a change to this file
alone. The test suite borrows ``validator()`` to check every widget against
the real published schema.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager

if TYPE_CHECKING:
    from a2ui.schema.catalog import A2uiCatalog
    from a2ui.schema.validator import A2uiValidator

# The version the SDK selects schemas by -- unprefixed, and *not* the ``"v0.9"``
# the wire ``version`` field carries (see ``render/components.py``'s
# ``A2UI_MESSAGE_VERSION``). The SDK is pre-1.0 and ships three specs -- 0.8,
# 0.9, and 0.9.1 (``a2ui/schema/constants.py``'s ``SPEC_VERSION_MAP``). This
# recipe targets 0.9 only, and the component vocabulary differs between them, so
# treat this as a deliberate pin rather than a default.
A2UI_VERSION = "0.9"


@lru_cache(maxsize=1)
def a2ui_catalog() -> A2uiCatalog:
    """The stock A2UI basic catalog for v0.9, loaded once per process."""
    manager = A2uiSchemaManager(
        version=A2UI_VERSION,
        catalogs=[BasicCatalog.get_config(A2UI_VERSION)],
    )
    return manager.get_selected_catalog()


def catalog_id() -> str:
    """The catalog id every ``createSurface`` message must declare."""
    return a2ui_catalog().catalog_id


def validator() -> A2uiValidator:
    """A validator for the catalog.

    Checks schema conformance plus surface integrity: unique ids, a
    reachable ``root``, no dangling references, no cycles, no orphans.
    """
    return a2ui_catalog().validator
