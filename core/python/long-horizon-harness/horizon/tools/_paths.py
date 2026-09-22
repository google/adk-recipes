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

"""Shared path-under-root resolver for tools that operate on the env root.

``file_ops``, ``bash``, and ``processes.terminal`` all need the same
trust-boundary check: resolve a possibly-relative path against the
active environment's ``working_dir`` and reject anything that escapes
via ``..`` or absolute paths outside the root. Each caller wraps the
error string in whatever envelope shape it returns.
"""

from __future__ import annotations

from pathlib import Path


def path_under_root(path: str, root: Path) -> tuple[Path | None, str | None]:
    """Resolve ``path`` relative to ``root`` and verify it stays inside.

    Relative paths join under ``root``; absolute paths must already be
    within it. On success returns ``(resolved, None)``; on escape
    returns ``(None, message)`` so callers can format their own error
    envelopes.
    """
    # Resolve root too: this is the single trust gate, and relative_to needs a
    # resolved root or a symlinked root yields a false-negative escape.
    root = root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"Path outside working_dir: {path} (env root: {root})"
    return resolved, None
