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

"""Per-session workspace window — a default scope + view, never a security boundary.

A session may anchor itself to one or more workspace-relative dirs (the
"window"). Path tools resolve their default (``.``) and relative names through
``resolve_in_window`` so they foreground the window instead of the whole shared
per-user workspace. The real trust gate stays ``horizon.tools._paths.path_under_root``
— it runs last on every resolution, so any in-root path (``/``-prefixed or naming
another subdir) still reaches the whole workspace. The window narrows the
*default*, never *access*.

Stored per-session in ``session.state`` (mirrors ``selected_model`` and the
per-session todo scoping). Absent/empty ⇒ whole workspace (back-compat).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

WORKSPACE_WINDOW_STATE_KEY = "workspace_window"

# A leading-slash path means "relative to the real workspace root" — the
# per-call escape hatch out of the window.
_ROOT_SENTINELS = frozenset({"", ".", "/"})


def _as_dict(state: Any) -> dict:
    if not state:
        return {}
    if isinstance(state, dict):
        return state
    if hasattr(state, "to_dict"):
        return state.to_dict()
    try:
        return dict(state)
    except Exception:
        return {}


def window_dirs(state: Any) -> list[str]:
    """The session's window dirs (workspace-relative POSIX), or ``[]``."""
    raw = _as_dict(state).get(WORKSPACE_WINDOW_STATE_KEY)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip().strip("/")
        if cleaned and cleaned != ".":
            out.append(cleaned)
    return out


def resolve_in_window(
    path: str, env_root: Path, window: list[str]
) -> tuple[Path | None, str | None]:
    """Resolve ``path`` with the window as the default scope.

    - ``.`` / ``""`` with a window set ⇒ the window's first dir (its anchor);
      with no window ⇒ ``env_root`` (today's behavior).
    - With NO window (e.g. ``scope="workspace"``), a bare root sentinel
      (``/``, ``.``, ``""``) resolves to ``env_root``; any other path goes
      straight to ``path_under_root`` (absolute-outside-root still rejected).
    - With a window set, a leading ``/`` is the per-call escape sentinel
      ("from the real root", e.g. ``/projB/x.md``), and any other relative
      name joins under the window's first dir.

    Always finishes via ``path_under_root(_, env_root)`` so the real security
    boundary is enforced regardless of the window.
    """
    # Lazy import: horizon.tools.__init__ imports this module, so a top-level
    # import here would be a cycle for any early importer (e.g. the API layer).
    from horizon.tools._paths import path_under_root

    anchor = window[0] if window else ""

    # No window ⇒ behave exactly as ``_resolve_under_env`` did: hand the raw
    # path to the trust gate, no escape-sentinel reinterpretation. This keeps
    # absolute-outside-root rejected and the no-window path fully back-compat.
    if not anchor:
        if path in _ROOT_SENTINELS:
            return path_under_root(".", env_root)
        return path_under_root(path, env_root)

    # A ``..`` component must never let a relative name climb out of the
    # anchor: joining ``projA/../escape`` can net back inside ``env_root`` and
    # slip past ``path_under_root``. Reject traversal outright — legitimate
    # break-out uses a leading ``/``, not ``..``.
    if ".." in Path(path).parts:
        return None, f"Path outside working_dir: {path} (env root: {env_root})"

    if path in _ROOT_SENTINELS:
        # ``/`` escapes to the real root; ``.``/``""`` resolve to the anchor.
        target = "." if path == "/" else anchor
        return path_under_root(target, env_root)

    if path.startswith("/"):
        # A genuine absolute path already under env_root (e.g. the caller
        # passed the resolved working_dir) is taken literally. Otherwise a
        # leading slash is the escape sentinel: "from the real root".
        if Path(path).is_absolute():
            literal, literal_err = path_under_root(path, env_root)
            if literal_err is None:
                return literal, None
        return path_under_root(path.lstrip("/") or ".", env_root)

    candidate = f"{anchor}/{path}"
    return path_under_root(candidate, env_root)


def render_window(window: list[str]) -> str | None:
    """One-line ``Focus`` annotation for the volatile reminder, or ``None``."""
    if not window:
        return None
    joined = ", ".join(f"`{d}/`" for d in window)
    return (
        f"Workspace focus: {joined}. Relative paths and `search_files` "
        "default to this scope. To reach the whole workspace, "
        "prefix a path with `/` (e.g. `/other-project/x.md`) or pass "
        '`scope="workspace"`. The user can clear the focus with `/workspace /`.'
    )


def maybe_seed_window(
    state: Any,
    resolved: Path,
    env_root: Path,
    *,
    check_host_fs: bool = True,
) -> None:
    """Seed an empty window to the top-level subdir a first write landed in.

    No-op when a window already exists, when ``state`` can't be written, or when
    the write is a root-level file (no project dir to anchor to).
    """
    if state is None:
        return
    if window_dirs(state):
        return
    try:
        rel = resolved.resolve().relative_to(env_root.resolve())
    except ValueError:
        return
    parts = rel.parts
    if len(parts) < 2:
        return
    top = parts[0]
    if check_host_fs and not (env_root.resolve() / top).is_dir():
        return
    try:
        state[WORKSPACE_WINDOW_STATE_KEY] = [top]
    except (TypeError, AttributeError):
        pass


__all__ = [
    "WORKSPACE_WINDOW_STATE_KEY",
    "maybe_seed_window",
    "render_window",
    "resolve_in_window",
    "window_dirs",
]
