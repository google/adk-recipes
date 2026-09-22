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

"""Per-backend capability descriptors.

Each model backend carries a ``ModelCapabilities`` (max inline-image bytes, a
``can_view_mime`` predicate, and an optional pre-dispatch ``prepare_contents``
hook). The dispatcher and the ``read`` tool's media path read the capabilities
by data, so neither names a concrete backend class: adding a model is one
descriptor entry,
and a model with unusual media limits or content quirks sets its own fields
instead of adding an ``isinstance`` branch.

Kept a leaf module (imports only genai types) so the registry can reference the
descriptors without an import cycle through the dispatcher.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from google.genai import types


@dataclass(frozen=True)
class ModelCapabilities:
    """What a backend can ingest and how the dispatcher must feed it."""

    # None = no inline-image byte cap the tool should pre-check.
    max_image_bytes: int | None
    # Can this backend ingest this inline mime type at all?
    can_view_mime: Callable[[str | None], bool]
    # Optional pre-dispatch content transform (sanitize/repair). None = passthrough.
    prepare_contents: (
        Callable[[list[types.Content]], list[types.Content]] | None
    )


def _views_anything(_mime: str | None) -> bool:
    return True


GEMINI_CAPABILITIES = ModelCapabilities(
    max_image_bytes=None,
    can_view_mime=_views_anything,
    prepare_contents=None,
)

# Unknown / directly-injected backend: assume it ingests what it's given and
# needs no repair (a well-behaved BaseLlm). Safe passthrough.
DEFAULT_CAPABILITIES = GEMINI_CAPABILITIES


__all__ = [
    "DEFAULT_CAPABILITIES",
    "GEMINI_CAPABILITIES",
    "ModelCapabilities",
]
