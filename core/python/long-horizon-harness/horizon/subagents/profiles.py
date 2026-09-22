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

"""Deny-by-default child archetypes for delegated sub-agents.

A ``ChildProfile`` with a non-None ``allowed_tool_names`` turns a child
into an allowlist sandbox: only the named tools may run, enforced by the
child-scoped ``before_tool_callback`` (see ``child_guard.py``). The
``"explore"`` profile is the read-only researcher — file reads + search,
no writes, no shell, no network.
"""

from __future__ import annotations

from dataclasses import dataclass

from horizon.tools import names


@dataclass(frozen=True)
class ChildProfile:
    name: str
    description: str
    allowed_tool_names: frozenset[str] | None


PROFILES: dict[str, ChildProfile] = {
    "explore": ChildProfile(
        name="explore",
        description=(
            "Read-only researcher: file reads and search only, no writes, "
            "no shell, no network."
        ),
        allowed_tool_names=frozenset({names.READ, names.SEARCH_FILES}),
    ),
}


def get_profile(name: str) -> ChildProfile:
    return PROFILES[name]


def render_profiles_block() -> str:
    header = "## Child profiles (pass profile=<name> to lock a child down)"
    lines = [
        f"- {p.name}: {p.description}"
        for p in sorted(PROFILES.values(), key=lambda x: x.name)
    ]
    return "\n".join([header, *lines])


__all__ = [
    "PROFILES",
    "ChildProfile",
    "get_profile",
    "render_profiles_block",
]
