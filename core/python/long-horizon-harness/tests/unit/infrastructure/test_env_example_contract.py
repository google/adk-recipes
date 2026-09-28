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

"""`.env.example` is a catalog of names, not a second copy of the defaults.

Both `make dev` and adk-recipes CI copy `.env.example` to `.env` and source it,
so seeding must not change behaviour. A bare ``KEY=`` exports an empty string,
which beats a code default under ``os.environ.get(KEY, "default")`` — hence the
two rules enforced here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The only values that may appear: deliberate local-dev choices, not restatements
# of a default that already lives in code.
DEV_CHOICES = {
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "LHA_ROOT_MODEL",
    "LHA_ENVIRONMENT_BACKEND",
    "USE_IN_MEMORY_SESSION",
    "LHA_AUTH_MODE",
    "LHA_DEV_USER_ID",
}

_ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_NONEMPTY_DEFAULT = re.compile(
    r"""(?:os\.environ\.get|os\.getenv)\(\s*["']([A-Z_][A-Z0-9_]*)["']\s*,\s*["'][^"']+["']"""
)


def _declared() -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGNMENT.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def test_only_dev_choices_carry_values() -> None:
    offenders = {
        k: v for k, v in _declared().items() if v and k not in DEV_CHOICES
    }
    assert not offenders, (
        "these restate a code default in .env.example; blank them and let the "
        f"code own the default: {sorted(offenders)}"
    )


def test_blank_declarations_are_read_empty_safe() -> None:
    """A var declared blank must not be read via ``get(name, "non-empty")`` —
    the blank would win over the default. Use ``env_str``/``env_flag`` instead."""
    blank = {k for k, v in _declared().items() if not v}
    offenders: list[str] = []
    for path in (REPO_ROOT / "horizon").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _NONEMPTY_DEFAULT.finditer(src):
            if m.group(1) in blank:
                line = src[: m.start()].count("\n") + 1
                offenders.append(
                    f"{m.group(1)} at {path.relative_to(REPO_ROOT)}:{line}"
                )
    assert not offenders, (
        "blank in .env.example but read with a non-empty default: "
        + str(offenders)
    )


@pytest.mark.parametrize("name", sorted(_declared()))
def test_declared_names_are_unique_and_upper(name: str) -> None:
    assert name == name.upper()
