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

"""Horizon environment interface: the ``Environment`` contract + its backends
(local host + Vertex sandbox REST client)."""

from typing import TYPE_CHECKING

from app.environment.base import Environment
from app.environment.local import LocalEnvironment

if TYPE_CHECKING:
    from app.environment.sandbox import SandboxEnvironment

__all__ = ["Environment", "LocalEnvironment", "SandboxEnvironment"]


def __getattr__(name: str) -> object:
    # Lazy so a bare ``import app.environment`` stays httpx-free; the sandbox
    # REST client is pulled only when actually referenced.
    if name == "SandboxEnvironment":
        from app.environment.sandbox import SandboxEnvironment

        return SandboxEnvironment
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
