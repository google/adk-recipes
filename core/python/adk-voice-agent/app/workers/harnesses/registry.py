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

"""Registry of available coding harnesses and default selection (`CODING_HARNESS`)."""

from __future__ import annotations

import os

from app.workers.harnesses.antigravity import AntigravityHarness
from app.workers.harnesses.base import CodingHarness
from app.workers.harnesses.claude import ClaudeCodeHarness

_FALLBACK_DEFAULT = "claude"


class HarnessRegistry:
    def __init__(self, harnesses: list[CodingHarness] | None = None) -> None:
        self._harnesses: dict[str, CodingHarness] = {}
        self._aliases: dict[str, str] = {}
        for harness in harnesses if harnesses is not None else _built_ins():
            self.register(harness)
        self._default_name = (
            self._canonical(os.getenv("CODING_HARNESS")) or _FALLBACK_DEFAULT
        )

    def register(self, harness: CodingHarness) -> None:
        name = _normalize(harness.name)
        self._harnesses[name] = harness
        for alias in harness.aliases:
            self._aliases[_normalize(alias)] = name

    def get(self, name: str | None = None) -> CodingHarness:
        if name is None:
            return self._harnesses[self._default_name]
        canonical = self._canonical(name)
        if canonical is None:
            raise ValueError(
                f"Unknown coding harness '{name}'. Available: {', '.join(self._harnesses)}."
            )
        return self._harnesses[canonical]

    def display_name(self, name: str) -> str:
        canonical = self._canonical(name)
        return self._harnesses[canonical].display_name if canonical else name

    def ref(self, name: str) -> dict[str, str]:
        canonical = self._canonical(name)
        if canonical is None:
            return {"name": name, "display_name": name, "icon": ""}
        harness = self._harnesses[canonical]
        return {
            "name": harness.name,
            "display_name": harness.display_name,
            "icon": harness.icon,
        }

    @property
    def default_harness(self) -> CodingHarness:
        return self._harnesses[self._default_name]

    def list_all(self) -> list[CodingHarness]:
        return list(self._harnesses.values())

    def _canonical(self, name: str | None) -> str | None:
        if not name:
            return None
        key = _normalize(name)
        return key if key in self._harnesses else self._aliases.get(key)


def _normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _built_ins() -> list[CodingHarness]:
    return [ClaudeCodeHarness(), AntigravityHarness()]


_harness_registry: HarnessRegistry | None = None


def get_harness_registry() -> HarnessRegistry:
    global _harness_registry
    if _harness_registry is None:
        _harness_registry = HarnessRegistry()
    return _harness_registry


def set_harness_registry(registry: HarnessRegistry | None) -> None:
    global _harness_registry
    _harness_registry = registry


def get_coding_harness(name: str | None = None) -> CodingHarness:
    return get_harness_registry().get(name)
