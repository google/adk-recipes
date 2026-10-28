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

"""The project's `.aqua/attached_aqua.json`, shared by the CLI and agents-cli."""

from __future__ import annotations

import json

import pytest
from ambient_quality_shared import attached_aqua


def test_saving_fields_keeps_the_other_writers_and_removes_none(tmp_path):
    attached_aqua.save_attached_aqua_fields(tmp_path, aqua_resource="engine")
    attached_aqua.save_attached_aqua_fields(
        tmp_path, agent_key="p__bot", agent_key_target="engine"
    )
    assert attached_aqua.load_attached_aqua(tmp_path) == {
        "aqua_resource": "engine",
        "agent_key": "p__bot",
        "agent_key_target": "engine",
    }

    attached_aqua.save_attached_aqua_fields(
        tmp_path, agent_key=None, agent_key_target=None
    )

    assert attached_aqua.load_attached_aqua(tmp_path) == {
        "aqua_resource": "engine"
    }


@pytest.mark.parametrize("text", ["not json", "[1]", b"\xff"])
def test_an_unreadable_record_is_empty(tmp_path, text):
    path = attached_aqua.build_attached_aqua_path(tmp_path)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")

    assert attached_aqua.load_attached_aqua(tmp_path) == {}


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("projects/p/x", "p__bot"),
        ("projects/p/x/", "p__bot"),
        ("projects/p/y", ""),
        ("", ""),
    ],
)
def test_the_recorded_key_resolves_only_for_its_aqua(
    tmp_path, target, expected
):
    attached_aqua.build_attached_aqua_path(tmp_path).write_text(
        json.dumps({"agent_key": "p__bot", "agent_key_target": "projects/p/x"}),
        encoding="utf-8",
    )

    assert (
        attached_aqua.resolve_recorded_agent_key(tmp_path, target) == expected
    )
