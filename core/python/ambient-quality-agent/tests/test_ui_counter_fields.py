# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests verifying parity between Python and TypeScript counter definitions.

Ensures the dashboard's TypeScript definitions in `ui/web` stay synchronized
with `InvestigationCounters` in Python, preventing missing counters or undefined
fields across the UI.
"""

from __future__ import annotations

import pathlib
import re

from ambient_quality_agent.tools.investigations.models import list_counter_names

_WEB = pathlib.Path(__file__).resolve().parents[1] / "ui" / "web"
_COUNTERS_TS = _WEB / "lib" / "counters.ts"
_API_TS = _WEB / "lib" / "aqua-api.ts"


def _counter_field_keys() -> list[str]:
    """Extracts keys from each entry in `COUNTER_FIELDS` in declaration order.

    Returns:
        List of counter field key strings.
    """
    body = _COUNTERS_TS.read_text().split("export const COUNTER_FIELDS", 1)[1]
    body = body.split("\n];", 1)[0]
    return re.findall(r'key:\s*"([a-z_]+)"', body)


def _interface_fields() -> list[str]:
    """Extracts field names declared in TypeScript `InvestigationCounters`.

    Returns:
        List of field name strings.
    """
    body = _API_TS.read_text().split(
        "export interface InvestigationCounters {", 1
    )[1]
    body = body.split("}", 1)[0]
    return re.findall(r"^\s*([a-z_]+): number;", body, re.M)


def test_typescript_interface_matches_the_model() -> None:
    assert _interface_fields() == list(list_counter_names())


def test_dashboard_labels_every_counter_the_model_defines() -> None:
    # Order too: the counter panel and the row chips read this list top to
    # bottom, and the pipeline's own order is the one that tells a story.
    assert _counter_field_keys() == list(list_counter_names())


def test_the_phantom_counter_is_gone() -> None:
    """Ensures `clusters_deferred` is absent from all counter definitions."""
    assert "clusters_deferred" not in list_counter_names()
    assert "clusters_deferred" not in _interface_fields()
    assert "clusters_deferred" not in _counter_field_keys()
