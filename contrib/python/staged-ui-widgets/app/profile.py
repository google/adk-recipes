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
"""The shopper profile: the third state scope, and the personalization input.

The profile lives under ``user:shopper_profile``. ADK persists ``user:``-
scoped keys across sessions, so a preference set today survives a restart --
which is the whole reason to demonstrate it alongside session registers and
``temp:`` flags. Three scopes, three lifetimes, one recipe:

======================  ==================  ============================
key                     scope               lives for
======================  ==================  ============================
``user:``               the shopper         forever, across sessions
``ui:register:*``       the session         the conversation
``temp:ui:dirty:*``     the invocation      one turn
======================  ==================  ============================

Preferences only. Purchase history is derived from ``store.orders()`` so the
two can never disagree.

Every write goes through ``update_preference``, which coerces and validates.
A model asked to fill a profile will confidently supply ``"about two
hundred"`` for a price ceiling, and a preference store that accepts it
produces a ranking that silently stops working.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

PROFILE_KEY = "user:shopper_profile"

# The starting profile. Hardcoded so the demo has something to personalize
# against on the very first turn -- an empty profile makes the first set of
# picks indistinguishable from a plain catalog listing, which hides the
# point.
DEFAULT_PROFILE: dict[str, Any] = {
    "display_name": "Rowan",
    "shoe_size": "9.5",
    "apparel_size": "M",
    "price_ceiling": 200.0,
    "favorite_brands": ["Aera", "Northbank"],
    "preferred_categories": ["trail-shoes", "mid-layer"],
    "colors": ["slate", "moss", "oat"],
    "avoid_materials": ["responsible down"],
    "needs_waterproof": False,
}


@dataclass(frozen=True)
class Field:
    """How one preference is stored and what it will accept."""

    kind: str  # "text" | "number" | "flag" | "list"
    label: str


EDITABLE_FIELDS: dict[str, Field] = {
    "shoe_size": Field("text", "shoe size"),
    "apparel_size": Field("text", "apparel size"),
    "price_ceiling": Field("number", "price ceiling"),
    "favorite_brands": Field("list", "favourite brands"),
    "preferred_categories": Field("list", "preferred categories"),
    "colors": Field("list", "colours"),
    "avoid_materials": Field("list", "materials to avoid"),
    "needs_waterproof": Field("flag", "waterproof requirement"),
}

_TRUE = {"true", "yes", "y", "1", "on", "required"}
_FALSE = {"false", "no", "n", "0", "off", "not required"}


def load_profile(state: Mapping[str, Any]) -> dict[str, Any]:
    """The shopper's profile, with defaults filled in for missing keys.

    Merging over the defaults rather than returning the stored dict means a
    profile written by an older version of this recipe still has every field
    the ranking expects.
    """
    stored = state.get(PROFILE_KEY)
    profile = dict(DEFAULT_PROFILE)
    if isinstance(stored, Mapping):
        for key, value in stored.items():
            if key in profile or key in EDITABLE_FIELDS:
                profile[key] = value
    return profile


def save_profile(
    state: MutableMapping[str, Any], profile: Mapping[str, Any]
) -> None:
    """Persists the profile under the ``user:``-scoped key."""
    state[PROFILE_KEY] = dict(profile)


class PreferenceError(ValueError):
    """A preference update that could not be applied as asked."""


def update_preference(
    state: MutableMapping[str, Any], field: str, value: Any
) -> tuple[str, Any]:
    """Validates one preference and writes the profile back.

    Returns the field's human label and the stored value, so the caller can
    confirm what actually changed rather than echoing what was requested.

    Raises ``PreferenceError`` for an unknown field or a value that cannot be
    coerced. Refusing beats storing ``"about two hundred"`` as a price
    ceiling and quietly ranking on nonsense from then on.
    """
    spec = EDITABLE_FIELDS.get(field)
    if spec is None:
        known = ", ".join(sorted(EDITABLE_FIELDS))
        raise PreferenceError(
            f"{field!r} is not an editable preference. Editable: {known}."
        )

    coerced = _coerce(spec, field, value)
    profile = load_profile(state)
    profile[field] = coerced
    save_profile(state, profile)
    return spec.label, coerced


def _coerce(spec: Field, field: str, value: Any) -> Any:
    if spec.kind == "number":
        return _as_number(field, value)
    if spec.kind == "flag":
        return _as_flag(field, value)
    if spec.kind == "list":
        return _as_list(field, value)
    return _as_text(field, value)


def _as_number(field: str, value: Any) -> float:
    if isinstance(value, bool):
        raise PreferenceError(f"{field} needs a number, not a yes/no.")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = str(value).strip().lstrip("$").replace(",", "")
        try:
            number = float(cleaned)
        except ValueError:
            raise PreferenceError(
                f"{field} needs a number; got {value!r}."
            ) from None
    if number <= 0:
        raise PreferenceError(f"{field} must be greater than zero.")
    return round(number, 2)


def _as_flag(field: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise PreferenceError(f"{field} needs a yes or a no; got {value!r}.")


def _as_list(field: str, value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        raise PreferenceError(
            f"{field} needs a list or a comma-separated string."
        )
    items = [p for p in parts if p]
    if not items:
        raise PreferenceError(f"{field} cannot be empty.")
    # Order-preserving dedupe: the first mention wins, which matches how
    # someone would say it.
    seen: set[str] = set()
    unique = []
    for item in items:
        lowered = item.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique.append(item)
    return unique


def _as_text(field: str, value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise PreferenceError(f"{field} cannot be empty.")
    return text
