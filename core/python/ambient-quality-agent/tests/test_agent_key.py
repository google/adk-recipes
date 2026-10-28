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

"""The agent key: its shape, and where AQuA's own project id comes from."""

from __future__ import annotations

import pytest
from ambient_quality_shared.agent_key import (
    build_agent_key,
    extract_agent_name,
    is_storage_key,
)

from .conftest import make_config


def test_the_agent_name_is_after_the_first_separator() -> None:
    """A project id holds no underscore, so an agent name with `__` in it is
    still split at the project's end."""
    key = build_agent_key("acme-prod", "it__support")

    assert key == "acme-prod__it__support"
    assert extract_agent_name(key) == "it__support"
    assert extract_agent_name("it_support_agent") == "it_support_agent"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("acme-prod__it_support_agent", True),
        # Kept by an agent in AQuA's own project until a rekey.
        ("it_support_agent", True),
        ("it-support-agent", False),
        ("acme-prod__it-support", False),
    ],
)
def test_an_attachment_is_stored_under_a_key_or_a_bare_name(
    value: str, expected: bool
) -> None:
    assert is_storage_key(value) is expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"project_id": "acme-aqua"}, "acme-aqua"),
        (
            {
                "project_id": "123456789012",
                "ambient_topic": "projects/acme-aqua/topics/t",
            },
            "acme-aqua",
        ),
        (
            {
                "project_id": "123456789012",
                "delay_task_queue": "projects/acme-aqua/locations/l/queues/q",
                "ambient_caller_sa": "caller@acme-aqua.iam.gserviceaccount.com",
            },
            "acme-aqua",
        ),
        ({"project_id": "123456789012"}, "123456789012"),
    ],
)
def test_aquas_project_id_replaces_the_number_agent_runtime_gives(
    fields: dict[str, str], expected: str
) -> None:
    assert make_config(**fields).resolve_aqua_project_id() == expected
