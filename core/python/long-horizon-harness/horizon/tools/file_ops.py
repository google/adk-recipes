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

"""File operations: read, write, edit, search.

active ``BaseEnvironment`` (``LocalEnvironment`` on the host,
``SandboxEnvironment`` for Agent Runtime sandboxes). Multi-hunk patch
parsing, file-locking, and in-memory checkpoint snapshots are out of scope.
"""

from __future__ import annotations

import itertools
import mimetypes
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from horizon.environment_context import active_environment
from horizon.models.media import sniff_image_or_pdf_mime
from horizon.tools._output_overflow import (
    TERMINAL_OUTPUT_LIMIT,
    TERMINAL_OUTPUT_MAX_LINES,
    make_preview,
    overflow_to_file,
)
from horizon.tools._paths import path_under_root
from horizon.tools._replacers import find_replacement
from horizon.workspace_window import (
    maybe_seed_window,
    resolve_in_window,
    window_dirs,
)

# Aliased to bash's cap, not an independent value: one uniform "no tool
# result exceeds 50KB, the rest is spilled" contract across read and bash.
_MAX_READ_CHARS = TERMINAL_OUTPUT_LIMIT
MAX_LINE_LENGTH = 2000
_LINE_TRUNC_SUFFIX = "... (line truncated)"
_MAX_DIAGNOSTICS = 20


def _error(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def _resolve_under_env(path: str) -> tuple[Path | None, str | None]:
    return path_under_root(path, active_environment().working_dir.resolve())


def _state_of(tool_context: Any | None) -> Any:
    return getattr(tool_context, "state", None)


def _resolve_in_session_window(
    path: str, tool_context: Any | None
) -> tuple[Path | None, str | None]:
    env_root = active_environment().working_dir.resolve()
    return resolve_in_window(
        path, env_root, window_dirs(_state_of(tool_context))
    )


def guess_mime(name: str) -> str:
    mime, _encoding = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def guess_mime_from_bytes(name: str, data: bytes | None) -> str:
    """Prefer the format the bytes actually are over what the extension claims.

    A file named ``.png`` that holds JPEG bytes (or a browser upload with an
    empty ``File.type``) is typed from its magic bytes so the model receives a
    correctly-typed blob and picks the right decoder. Falls back to the
    extension when the magic bytes aren't recognized.
    """
    return sniff_image_or_pdf_mime(data) or guess_mime(name)


_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tiff",
        ".tif",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".wmv",
        ".flv",
        ".m4v",
        ".mpeg",
        ".mpg",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".aac",
        ".m4a",
        ".wma",
        ".aiff",
        ".opus",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".7z",
        ".rar",
        ".xz",
        ".z",
        ".tgz",
        ".iso",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".o",
        ".a",
        ".obj",
        ".lib",
        ".app",
        ".msi",
        ".deb",
        ".rpm",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        ".pyc",
        ".pyo",
        ".class",
        ".jar",
        ".war",
        ".ear",
        ".node",
        ".wasm",
        ".rlib",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".mdb",
        ".idx",
        ".psd",
        ".ai",
        ".eps",
        ".sketch",
        ".fig",
        ".xd",
        ".blend",
        ".3ds",
        ".max",
        ".swf",
        ".fla",
        ".lockb",
        ".dat",
        ".data",
    }
)


def _has_binary_extension(path: str) -> bool:
    return Path(path).suffix.lower() in _BINARY_EXTENSIONS


def _denied_exact_paths(home: str) -> set[str]:
    return {
        os.path.realpath(p)
        for p in (
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        )
    }


def _denied_prefixes(home: str) -> list[str]:
    return [
        os.path.realpath(p) + os.sep
        for p in (
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        )
    ]


def _is_write_denied(path: str) -> bool:
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    if resolved in _denied_exact_paths(home):
        return True
    return any(resolved.startswith(prefix) for prefix in _denied_prefixes(home))


_SEARCH_DELIMITER = "###__LHA_SEARCH_RESULT__###"
_SEARCH_ERROR_KEY = "__lha_search_error__"
_SEARCH_MAX_FILE_SIZE = 10 * 1024 * 1024
_SEARCH_SNIPPET_MAX_CHARS = 300
_SEARCH_IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "coverage",
        "target",
        ".next",
        ".terraform",
        "vendor",
        "site-packages",
    }
)


def _build_search_script(
    root: str,
    pattern: str,
    file_glob: str | None,
    limit: int,
    ignore_case: bool = False,
    no_ignore: bool = False,
) -> str:
    return (
        "import fnmatch, json, os, re, sys\n"
        f"_root = {root!r}\n"
        f"_pattern = {pattern!r}\n"
        f"_file_glob = {file_glob!r}\n"
        f"_limit = {limit!r}\n"
        f"_ignore_case = {ignore_case!r}\n"
        f"_no_ignore = {no_ignore!r}\n"
        f"_error_key = {_SEARCH_ERROR_KEY!r}\n"
        f"_delim = {_SEARCH_DELIMITER!r}\n"
        f"_binary = {set(_BINARY_EXTENSIONS)!r}\n"
        f"_ignored = {set(_SEARCH_IGNORED_DIRS)!r}\n"
        f"_max_size = {_SEARCH_MAX_FILE_SIZE!r}\n"
        f"_snip_max = {_SEARCH_SNIPPET_MAX_CHARS!r}\n"
        "try:\n"
        "    _re = re.compile(_pattern, re.IGNORECASE if _ignore_case else 0)\n"
        "except re.error as _exc:\n"
        "    print(_delim + json.dumps({_error_key: str(_exc)}) + _delim)\n"
        "    sys.exit(0)\n"
        "if not os.path.isdir(_root):\n"
        "    sys.stderr.write('search path not a directory: ' + _root + '\\n')\n"
        "    sys.exit(2)\n"
        "_matches = []\n"
        "for _dp, _dns, _fns in os.walk(_root):\n"
        "    _dns[:] = [d for d in _dns if not d.startswith('.') and (_no_ignore or d not in _ignored)]\n"
        "    for _fn in _fns:\n"
        "        if _fn.startswith('.'):\n"
        "            continue\n"
        "        if _file_glob and not fnmatch.fnmatch(_fn, _file_glob):\n"
        "            continue\n"
        "        _dot = _fn.rfind('.')\n"
        "        if _dot != -1 and _fn[_dot:].lower() in _binary:\n"
        "            continue\n"
        "        _full = os.path.join(_dp, _fn)\n"
        "        try:\n"
        "            _st = os.stat(_full)\n"
        "            if _st.st_size > _max_size:\n"
        "                continue\n"
        "            _mtime = _st.st_mtime\n"
        "        except OSError:\n"
        "            continue\n"
        "        try:\n"
        "            with open(_full, 'r', encoding='utf-8', errors='replace') as _fh:\n"
        "                for _ln, _line in enumerate(_fh, start=1):\n"
        "                    _line = _line.rstrip('\\n')\n"
        "                    _m = _re.search(_line)\n"
        "                    if _m:\n"
        "                        _left = max(0, _m.start() - 100)\n"
        "                        _snip = _line[_left:_left + _snip_max]\n"
        "                        if _left > 0:\n"
        "                            _snip = '...' + _snip\n"
        "                        if _left + _snip_max < len(_line):\n"
        "                            _snip = _snip + '...'\n"
        "                        _matches.append({'path': _full, 'line': _ln, 'text': _snip, 'mtime': _mtime})\n"
        "                        if len(_matches) >= _limit:\n"
        "                            break\n"
        "        except OSError:\n"
        "            continue\n"
        "        if len(_matches) >= _limit:\n"
        "            break\n"
        "    if len(_matches) >= _limit:\n"
        "        break\n"
        "print(_delim + json.dumps(_matches) + _delim)\n"
    )


def extract_delimited(text: str, delimiter: str) -> str | None:
    """Pulls an embedded script's JSON payload out of its stdout, fenced by a
    sentinel the surrounding shell noise won't contain."""
    first = text.find(delimiter)
    if first == -1:
        return None
    after = first + len(delimiter)
    second = text.find(delimiter, after)
    if second == -1:
        return None
    return text[after:second]


def _format_numbered_lines(
    window: list[str], first_line_no: int, total_lines: int
) -> str:
    rendered: list[str] = []
    for i, raw_line in enumerate(window):
        line = raw_line
        if len(line) > MAX_LINE_LENGTH:
            line = raw_line[:MAX_LINE_LENGTH] + _LINE_TRUNC_SUFFIX
        rendered.append(f"{first_line_no + i}: {line}")

    body = "\n".join(rendered)
    last_line_no = first_line_no + len(window) - 1
    if last_line_no >= total_lines:
        trailer = f"End of file - total {total_lines} lines"
    else:
        trailer = (
            f"Showing lines {first_line_no}-{last_line_no} of {total_lines}. "
            f"Use offset={last_line_no + 1} to continue."
        )
    return f"{body}\n\n{trailer}"


async def read_file(
    path: str,
    *,
    offset: int = 1,
    limit: int = 500,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Read a text file, optionally paginated by 1-indexed line offset.

    Relative paths resolve under your current workspace focus (see
    `/workspace`); prefix with `/` to reach the whole workspace.
    """
    import asyncio

    target, err = _resolve_in_session_window(path, tool_context)
    if err is not None:
        return _error(err)
    assert target is not None

    if _is_write_denied(str(target)):
        return _error(f"Read denied: {path} is on the protected paths list.")

    if _has_binary_extension(str(target)):
        return _error(f"Cannot read binary file: {path}")

    # Reject non-regular files (FIFOs, sockets, character devices) when the
    # path resolves on the host. ``Path.read_bytes`` on `/dev/urandom` would
    # block forever — better to fail fast. The check is skipped when the
    # path does not exist on the host (e.g. ``/workspace/...`` while
    # running against a SandboxEnvironment), where the env handles errors.
    if os.path.lexists(target) and not os.path.isfile(target):
        if os.path.isdir(target):
            return _error(f"Path is a directory, not a file: {path}")
        return _error(f"Not a regular file: {path}")

    try:
        raw = await active_environment().read_file(target)
    except FileNotFoundError:
        return _error(f"File not found: {path}")
    except IsADirectoryError:
        return _error(f"Path is a directory, not a file: {path}")
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except OSError as exc:
        return _error(f"Failed to read {path}: {exc}")

    text = raw.decode("utf-8", errors="replace")
    if text == "":
        return {"success": True, "content": ""}

    lines = text.splitlines()
    total_lines = len(lines)
    start = max(offset - 1, 0)
    if start >= total_lines:
        return {"success": True, "content": ""}
    end = start + max(limit, 0)
    window = lines[start:end]

    body = _format_numbered_lines(window, start + 1, total_lines)
    _, would_overflow = make_preview(
        body, max_bytes=_MAX_READ_CHARS, max_lines=TERMINAL_OUTPUT_MAX_LINES
    )
    if not would_overflow:
        return {"success": True, "content": body}

    # Same contract as bash: spill the text, return a preview plus a
    # pointer, instead of a bare slice that silently drops the tail. Only
    # the requested page is spilled here (offset/limit already scope the
    # file), so the pointer says "this page", not "full output".
    overflow = await overflow_to_file(
        body,
        stream="read",
        max_bytes=_MAX_READ_CHARS,
        max_lines=TERMINAL_OUTPUT_MAX_LINES,
        label="This page of output",
    )
    return {
        "success": True,
        "content": f"{overflow.preview}\n\n{overflow.pointer}",
        "truncated": True,
        "overflow_path": overflow.path,
    }


async def write(
    path: str, content: str, *, tool_context: Any | None = None
) -> dict[str, Any]:
    """Write content to path, creating parent directories as needed.
    Paths: see routing.
    """
    target, err = _resolve_in_session_window(path, tool_context)
    if err is not None:
        return _error(err)
    assert target is not None

    if _is_write_denied(str(target)):
        return _error(f"Write denied: {path} is on the protected paths list.")

    env = active_environment()
    try:
        await env.write_file(target, content)
    except OSError as exc:
        return _error(f"Failed to write {path}: {exc}")

    maybe_seed_window(
        _state_of(tool_context),
        target,
        env.working_dir.resolve(),
        check_host_fs=env.on_host_fs,
    )

    result: dict[str, Any] = {"success": True, "path": str(target)}
    if str(target).endswith(".py"):
        diagnostics = await _post_edit_diagnostics(str(target))
        if diagnostics:
            result["diagnostics"] = diagnostics
    return result


async def _post_edit_diagnostics(path: str) -> str | None:
    """Best-effort ruff diagnostics for a just-written ``.py`` file.

    Returns a model-facing ``file:line: code message`` block (capped), or
    ``None`` when ruff is unavailable or produced nothing actionable. Never
    raises — a lint failure must not turn a successful edit into an error.
    """
    import asyncio
    import json

    command = (
        f"ruff check --output-format=json --force-exclude {shlex.quote(path)}"
    )
    try:
        result = await active_environment().execute(command, timeout=30.0)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:
        return None

    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    try:
        findings = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(findings, list) or not findings:
        return None

    lines: list[str] = []
    for item in findings[:_MAX_DIAGNOSTICS]:
        if not isinstance(item, dict):
            continue
        loc = item.get("location") or {}
        row = loc.get("row", "?")
        code = item.get("code") or ""
        message = item.get("message", "")
        prefix = f"{code} " if code else ""
        lines.append(f"{path}:{row}: {prefix}{message}".rstrip())

    if not lines:
        return None

    overflow = len(findings) - len(lines)
    body = "\n".join(lines)
    if overflow > 0:
        body += f"\n... and {overflow} more"
    return "ruff diagnostics (informational — the edit was applied):\n" + body


@dataclass(frozen=True)
class _ResolvedEdit:
    """One edits[] item, resolved to its exact span in the ORIGINAL content
    (matches are computed against the file as read, never against a
    progressively-updated copy — that is what makes "non-overlapping"
    checkable before anything is applied)."""

    start: int
    end: int
    new_text: str
    index: int


def _resolve_edits(
    original: str, edits: list[Any]
) -> tuple[list[_ResolvedEdit] | None, str | None]:
    resolved: list[_ResolvedEdit] = []
    for i, item in enumerate(edits):
        if not isinstance(item, dict):
            return (
                None,
                f"edits[{i}] must be an object with oldText and newText",
            )
        old_text = item.get("oldText")
        new_text = item.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return None, f"edits[{i}] must have string oldText and newText"

        resolution = find_replacement(original, old_text)
        if resolution.error is not None:
            return None, f"edits[{i}]: {resolution.error}"
        assert resolution.search is not None

        match_start = original.find(resolution.search)
        resolved.append(
            _ResolvedEdit(
                start=match_start,
                end=match_start + len(resolution.search),
                new_text=new_text,
                index=i,
            )
        )

    resolved.sort(key=lambda r: r.start)
    for a, b in itertools.pairwise(resolved):
        if b.start < a.end:
            return None, (
                f"edits[{a.index}] and edits[{b.index}] target overlapping or "
                "adjacent regions: merge them into one edit"
            )
    return resolved, None


def _apply_resolved_edits(original: str, resolved: list[_ResolvedEdit]) -> str:
    pieces: list[str] = []
    cursor = 0
    for r in resolved:
        pieces.append(original[cursor : r.start])
        pieces.append(r.new_text)
        cursor = r.end
    pieces.append(original[cursor:])
    return "".join(pieces)


async def edit(
    path: str,
    edits: list[dict[str, str]],
    *,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Apply one or more {oldText, newText} edits to path atomically —
    untouched if any oldText isn't a unique match or two edits overlap.
    Merge nearby changes into one edit rather than two overlapping ones.
    Paths: see routing.
    """
    target, err = _resolve_in_session_window(path, tool_context)
    if err is not None:
        return _error(err)
    assert target is not None

    if _is_write_denied(str(target)):
        return _error(f"Write denied: {path} is on the protected paths list.")

    if not edits:
        return _error("edits must be a non-empty list of {oldText, newText}.")

    env = active_environment()
    try:
        raw = await env.read_file(target)
    except FileNotFoundError:
        return _error(f"File not found: {path}")
    except IsADirectoryError:
        return _error(f"Path is a directory, not a file: {path}")
    except OSError as exc:
        return _error(f"Failed to read {path}: {exc}")

    original = raw.decode("utf-8", errors="replace")

    resolved, resolve_err = _resolve_edits(original, edits)
    if resolve_err is not None:
        return _error(f"{resolve_err} (file: {path})")
    assert resolved is not None

    updated = _apply_resolved_edits(original, resolved)

    try:
        await env.write_file(target, updated)
    except OSError as exc:
        return _error(f"Failed to write {path}: {exc}")

    maybe_seed_window(
        _state_of(tool_context),
        target,
        env.working_dir.resolve(),
        check_host_fs=env.on_host_fs,
    )

    result: dict[str, Any] = {
        "success": True,
        "path": str(target),
        "replacements": len(resolved),
    }
    if str(target).endswith(".py"):
        diagnostics = await _post_edit_diagnostics(str(target))
        if diagnostics:
            result["diagnostics"] = diagnostics
    return result


async def search_files(
    pattern: str,
    *,
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    scope: str = "window",
    ignore_case: bool = False,
    no_ignore: bool = False,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Search text files for `pattern` (regex); scope="workspace" searches
    everywhere. Paths: see routing.

    Returns the first `limit` matches in directory-walk order, sorted by
    mtime (most recent first): on a large tree this is a prefix of the
    walk, not a global top-N.
    """
    import asyncio
    import json

    env_root = active_environment().working_dir.resolve()
    window = (
        [] if scope == "workspace" else window_dirs(_state_of(tool_context))
    )
    root, err = resolve_in_window(path, env_root, window)
    if err is not None:
        return _error(err)
    assert root is not None

    script = _build_search_script(
        str(root),
        pattern,
        file_glob,
        limit,
        ignore_case=ignore_case,
        no_ignore=no_ignore,
    )
    command = f"python3 -c {shlex.quote(script)}"
    try:
        result = await active_environment().execute(command, timeout=30.0)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except TimeoutError:
        return _error(
            f"Search timed out after 30s for {path}. Narrow the path or pattern."
        )
    except Exception as exc:
        return _error(f"Search failed for {path}: {exc}")

    if result.exit_code != 0:
        msg = result.stderr.strip() or "search failed"
        if "not a directory" in msg.lower() or "No such file" in msg:
            return _error(f"Search path invalid: {path}: {msg}")
        return _error(f"Search failed for {path}: {msg}")

    payload = extract_delimited(result.stdout, _SEARCH_DELIMITER)
    if payload is None:
        return _error(
            f"Search produced no result envelope for {path}; stderr={result.stderr!r}"
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        return _error(f"Search produced invalid JSON: {exc}")

    if isinstance(parsed, dict) and _SEARCH_ERROR_KEY in parsed:
        return _error(f"Invalid regex pattern: {parsed[_SEARCH_ERROR_KEY]}")

    is_truncated = len(parsed) >= limit
    sorted_matches = sorted(
        parsed, key=lambda m: m.get("mtime", 0), reverse=True
    )
    matches = [
        {"path": m["path"], "line": m["line"], "text": m["text"]}
        for m in sorted_matches
    ]
    return {
        "success": True,
        "matches": matches,
        "truncated": is_truncated,
    }
