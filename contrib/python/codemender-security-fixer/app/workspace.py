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
"""Target selection, throwaway working copies and path safety.

The target (bundled sample or TARGET_DIR) is never written to. The agent
edits a temporary working copy, and every candidate patch is validated in a
second, disposable copy.
"""

import difflib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

BUNDLED_TARGET = Path(__file__).resolve().parent / "sample_target"
MAX_FILE_CHARS = 60_000
MAX_TARGET_FILES = 2_000
MAX_TARGET_BYTES = 50 * 1024 * 1024
_IGNORED = shutil.ignore_patterns(
    ".git",
    ".hg",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".env",
)


class WorkspaceError(ValueError):
    """Raised for an unusable target or an unsafe path."""


def resolve_target(target_dir: str | None = None) -> Path:
    """Returns the directory to fix: `target_dir`, else the bundled sample."""
    path = (
        Path(target_dir).expanduser().resolve()
        if target_dir
        else BUNDLED_TARGET
    )
    if not path.is_dir():
        raise WorkspaceError(f"target directory not found: {path}")
    return path


def copy_tree(source: Path, dest_parent: str) -> Path:
    """Copies `source` into `dest_parent/target`, enforcing size limits."""
    dest = Path(dest_parent) / "target"
    shutil.copytree(source, dest, symlinks=True, ignore=_IGNORED)
    files, size = 0, 0
    for root, _dirs, names in os.walk(dest):
        for name in names:
            files += 1
            path = Path(root) / name
            if not path.is_symlink():
                size += path.stat().st_size
    if files > MAX_TARGET_FILES or size > MAX_TARGET_BYTES:
        shutil.rmtree(dest_parent, ignore_errors=True)
        raise WorkspaceError(
            f"target too large ({files} files, {size} bytes); limits are "
            f"{MAX_TARGET_FILES} files and {MAX_TARGET_BYTES} bytes"
        )
    return dest


def create_working_copy(source: Path, applied: dict[str, str]) -> Path:
    """Creates a fresh working copy of `source` and re-applies accepted edits.

    Replaying `applied` makes the working copy reproducible from session
    state alone, e.g. when a request lands on a different server instance.
    """
    work = copy_tree(source, tempfile.mkdtemp(prefix="codemender-work-"))
    for rel, content in applied.items():
        write_file(work, rel, content)
    return work


def is_test_path(rel_path: str) -> bool:
    """True for test files, which the agent may read but never modify."""
    parts = PurePosixPath(rel_path).parts
    name = parts[-1] if parts else ""
    return (
        any(p in {"tests", "test"} for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def safe_path(root: Path, rel_path: str, must_exist: bool = True) -> Path:
    """Resolves `rel_path` inside `root`, refusing anything that escapes it."""
    rel = PurePosixPath(rel_path.strip())
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise WorkspaceError(f"path must be relative to the target: {rel_path}")
    path = root / rel
    if path.is_symlink():
        raise WorkspaceError(f"refusing to follow symlink: {rel_path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise WorkspaceError(f"path escapes the target: {rel_path}")
    if must_exist and not path.is_file():
        raise WorkspaceError(f"file not found in target: {rel_path}")
    return path


def read_file(root: Path, rel_path: str) -> str:
    path = safe_path(root, rel_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        raise WorkspaceError(f"file exceeds {MAX_FILE_CHARS} characters")
    return text


def write_file(root: Path, rel_path: str, content: str) -> None:
    safe_path(root, rel_path).write_text(content, encoding="utf-8")


def list_files(root: Path, suffix: str = ".py") -> list[str]:
    """Lists files under `root` (relative, POSIX style) with `suffix`."""
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(suffix):
                rel = (Path(dirpath) / name).relative_to(root)
                out.append(rel.as_posix())
    return sorted(out)


def _lines(text: str) -> list[str]:
    return (
        text if text.endswith("\n") or not text else text + "\n"
    ).splitlines(True)


def unified_diff(source: Path, applied: dict[str, str]) -> str:
    """A `git apply`-compatible diff from the original target to `applied`."""
    chunks = []
    for rel in sorted(applied):
        before = _lines((source / rel).read_text(encoding="utf-8"))
        after = _lines(applied[rel])
        chunks.extend(
            difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}")
        )
    return "".join(chunks)
