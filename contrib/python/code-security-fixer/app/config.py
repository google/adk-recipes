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
"""Validates configuration read from the environment.

Call sites read variables with a direct `os.getenv("NAME")` (so the repo's
env-var checker sees them) and pass the value here. Defaults live in
.env.example, never in code.
"""


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


def require(name: str, value: str | None) -> str:
    """Returns `value` (may be empty), or raises if `name` was not set."""
    if value is None:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env (or set it in the"
            " service environment)."
        )
    return value


def require_number(
    name: str, value: str | None, kind: type[int] | type[float]
) -> int | float:
    """Returns `value` parsed as `kind` (int or float)."""
    raw = require(name, value)
    try:
        return kind(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
