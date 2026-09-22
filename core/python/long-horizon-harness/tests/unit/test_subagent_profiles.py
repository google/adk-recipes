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

"""Child-profile registry: deny-by-default archetypes for delegated children."""

from __future__ import annotations

import pytest


def test_explore_profile_is_read_only() -> None:
    from horizon.subagents.profiles import get_profile

    explore = get_profile("explore")
    # view_file was merged into read (Task 3); the merged read tool's media
    # path (as_media=True) absorbs what view_file used to cover.
    assert explore.allowed_tool_names == frozenset({"read", "search_files"})
    # Read-only means no writes, no shell, no network in the allowlist.
    for forbidden in (
        "write_file",
        "patch",
        "terminal",
        "process",
        "web_research",
    ):
        assert forbidden not in explore.allowed_tool_names


def test_get_profile_unknown_raises_keyerror() -> None:
    from horizon.subagents.profiles import get_profile

    with pytest.raises(KeyError):
        get_profile("does-not-exist")


def test_render_profiles_block_lists_each_profile() -> None:
    from horizon.subagents.profiles import render_profiles_block

    block = render_profiles_block()
    assert "## Child profiles" in block
    assert "- explore:" in block
    assert "Read-only" in block
