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

from __future__ import annotations

from pathlib import Path
from typing import Any

from horizon.workspace_window import (
    WORKSPACE_WINDOW_STATE_KEY,
    maybe_seed_window,
    render_window,
    resolve_in_window,
    window_dirs,
)


def test_window_dirs_absent_is_empty():
    assert window_dirs({}) == []


def test_window_dirs_reads_list():
    assert window_dirs(
        {WORKSPACE_WINDOW_STATE_KEY: ["projA", "reports/q2"]}
    ) == [
        "projA",
        "reports/q2",
    ]


def test_window_dirs_coerces_bare_string():
    # Leniency: a single string is treated as one dir.
    assert window_dirs({WORKSPACE_WINDOW_STATE_KEY: "projA"}) == ["projA"]


def test_window_dirs_drops_empty_and_dot_entries():
    assert window_dirs({WORKSPACE_WINDOW_STATE_KEY: ["", ".", "projA"]}) == [
        "projA"
    ]


def test_resolve_default_dot_with_no_window_is_root(tmp_path: Path):
    resolved, err = resolve_in_window(".", tmp_path, [])
    assert err is None
    assert resolved == tmp_path


def test_resolve_slash_with_no_window_is_root(tmp_path: Path):
    # scope="workspace" -> window=[]; a bare "/" must mean the workspace root,
    # not the filesystem root (search_files' docstring promises path="/" and
    # scope="workspace" search the whole workspace).
    resolved, err = resolve_in_window("/", tmp_path, [])
    assert err is None
    assert resolved == tmp_path


def test_resolve_default_dot_with_window_is_first_dir(tmp_path: Path):
    resolved, err = resolve_in_window(".", tmp_path, ["projA"])
    assert err is None
    assert resolved == (tmp_path / "projA").resolve()


def test_resolve_relative_name_joins_under_window(tmp_path: Path):
    resolved, err = resolve_in_window("note.md", tmp_path, ["projA"])
    assert err is None
    assert resolved == (tmp_path / "projA" / "note.md").resolve()


def test_resolve_slash_escapes_to_root(tmp_path: Path):
    resolved, err = resolve_in_window("/", tmp_path, ["projA"])
    assert err is None
    assert resolved == tmp_path


def test_resolve_other_subdir_escapes_window_but_stays_in_root(tmp_path: Path):
    # In-root escape is allowed — the window is not a security boundary.
    resolved, err = resolve_in_window("/projB/x.md", tmp_path, ["projA"])
    assert err is None
    assert resolved == (tmp_path / "projB" / "x.md").resolve()


def test_resolve_dotdot_still_blocked_by_path_under_root(tmp_path: Path):
    resolved, err = resolve_in_window("../escape", tmp_path, ["projA"])
    assert resolved is None
    assert err is not None


def test_render_window_empty_is_none():
    assert render_window([]) is None


def test_render_window_lists_dirs():
    assert "projA" in (render_window(["projA"]) or "")


def test_maybe_seed_window_sandbox_off_host_root():
    """When check_host_fs=False (sandbox), non-host root paths seed correctly."""
    state: dict[str, Any] = {}
    maybe_seed_window(
        state,
        Path("/workspace/project_a/main.py"),
        Path("/workspace"),
        check_host_fs=False,
    )
    assert state.get(WORKSPACE_WINDOW_STATE_KEY) == ["project_a"]


def test_maybe_seed_window_default_requires_host_dir():
    """Default check_host_fs=True does not seed if directory is absent on host."""
    state: dict[str, Any] = {}
    maybe_seed_window(
        state,
        Path("/nonexistent_sandbox_root/proj/app.py"),
        Path("/nonexistent_sandbox_root"),
    )
    assert WORKSPACE_WINDOW_STATE_KEY not in state


def test_maybe_seed_window_root_level_file_does_not_seed(tmp_path: Path):
    """Root-level files with no subdirectory component must not seed window."""
    root = tmp_path.resolve()
    state_host: dict[str, Any] = {}
    maybe_seed_window(
        state_host,
        root / "README.md",
        root,
        check_host_fs=True,
    )
    assert WORKSPACE_WINDOW_STATE_KEY not in state_host

    state_sandbox: dict[str, Any] = {}
    maybe_seed_window(
        state_sandbox,
        Path("/workspace/README.md"),
        Path("/workspace"),
        check_host_fs=False,
    )
    assert WORKSPACE_WINDOW_STATE_KEY not in state_sandbox


def test_maybe_seed_window_existing_window_not_overwritten(tmp_path: Path):
    """An existing window is never overwritten."""
    root = tmp_path.resolve()
    (root / "new_proj").mkdir()
    state: dict[str, Any] = {WORKSPACE_WINDOW_STATE_KEY: ["existing_proj"]}
    maybe_seed_window(
        state,
        root / "new_proj" / "app.py",
        root,
    )
    assert state[WORKSPACE_WINDOW_STATE_KEY] == ["existing_proj"]
