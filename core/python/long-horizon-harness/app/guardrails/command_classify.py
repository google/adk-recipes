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

"""Shell command parsing for the permission gate.

Decides the auto-scope prefix shown on the approval card and splits chained
commands so a benign segment can't smuggle a gated one (``ls && bq rm``).
"""

from __future__ import annotations

import re

_WRAPPER_RE = re.compile(
    r"""^(?:bash|sh|zsh)\s+-c\s+(['"])(.*)\1\s*$""", re.DOTALL
)
_REDIRECTION_RE = re.compile(r"(>>|>|<)")
_BARE_WORD_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# `sudo` is deliberately absent: unwrapping it would let a plain `npm install`
# grant cover `sudo npm install`, and a grant skips the command_safety
# privilege-escalation demotion. The prefix must name the escalation.
_SKIP_PREFIXES = frozenset({"env"})


def strip_wrapper(command: str) -> str:
    """Unwrap a single ``bash -c '...'`` / ``sh -c "..."`` shell wrapper."""
    m = _WRAPPER_RE.match(command.strip())
    return m.group(2).strip() if m else command.strip()


def split_segments(command: str) -> list[str]:
    """Split on top-level ``&&`` / ``||`` / ``|`` / ``;`` / bare ``&`` / newline
    into trimmed, non-empty parts.

    Quote-aware: operators inside single/double quotes are literal, so a quoted
    interpreter body (``python3 -c "a; b | c"``) stays one segment instead of
    being shredded into a fake prefix per source line. A lone ``&`` (background)
    splits, but not an fd-dup/redirect ``&`` (``2>&1``, ``&>out``) that sits
    next to a ``<``/``>``/``&``.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_single = in_double = False
    i, n = 0, len(command)

    def flush() -> None:
        seg = "".join(buf).strip()
        if seg:
            parts.append(seg)
        buf.clear()

    while i < n:
        ch = command[i]
        if in_single:
            buf.append(ch)
            in_single = ch != "'"
            i += 1
        elif in_double:
            if ch == "\\" and i + 1 < n:
                buf.append(ch + command[i + 1])
                i += 2
            else:
                buf.append(ch)
                in_double = ch != '"'
                i += 1
        elif ch == "\\" and i + 1 < n:
            buf.append(ch + command[i + 1])
            i += 2
        elif ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
        elif ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
        elif ch in ";\n":
            flush()
            i += 1
        elif ch == "&":
            # Slice form (not command[i-1]) so i==0 yields '' — a leading `&` has
            # no adjacent fd-dup char and stays a bare-background split.
            prev, nxt = command[i - 1 : i], command[i + 1 : i + 2]
            if nxt == "&":
                flush()
                i += 2
            elif prev not in ("<", ">", "&") and nxt not in ("<", ">", "&"):
                flush()  # bare background &
                i += 1
            else:  # fd-dup / &>file redirect
                buf.append(ch)
                i += 1
        elif ch == "|":
            flush()
            i += 2 if command[i + 1 : i + 2] == "|" else 1
        else:
            buf.append(ch)
            i += 1
    flush()
    return parts


def _skip_leading(tokens: list[str]) -> int:
    """Index of the binary: past leading VAR=val assignments and env wrappers."""
    i = 0
    while i < len(tokens) and ("=" in tokens[i] or tokens[i] in _SKIP_PREFIXES):
        i += 1
    return i


def significant_tokens(segment: str) -> list[str]:
    """Binary + positional tokens, past wrappers and self-contained flags.

    ``bq --project_id=X query …`` → ``["bq", "query", …]``, so a saved prefix
    matches wherever the flags sit. Scanning **stops at a bare flag** (``-n``,
    ``--context``): it may consume the next token as its value and nothing here
    knows which CLI does that, so ``kubectl -n get delete …`` yields ``["kubectl"]``
    rather than letting a namespace named ``get`` impersonate the subcommand.
    """
    tokens = segment.split()
    positional: list[str] = []
    for token in tokens[_skip_leading(tokens) :]:
        if token.startswith("-"):
            if "=" in token:
                continue
            break
        positional.append(token)
    return positional


def command_prefix(segment: str) -> str:
    """Binary + first bare-word subcommand token (the auto-scope prefix)."""
    # Same token walk as the matcher, so a derived prefix always matches the
    # command it came from: `bq --project_id=X query` scopes to `bq query`, not to
    # the whole `bq` binary (which would carry `bq rm` with it, since a grant
    # skips the command_safety demotion).
    positional = significant_tokens(segment)
    if not positional:
        return segment.strip()
    binary = positional[0]
    nxt = positional[1] if len(positional) > 1 else None
    # Only a plain-name binary takes a bare-word subcommand; a path-like binary
    # (e.g. ``./run.sh``) has positional args, not subcommands.
    if (
        _BARE_WORD_RE.match(binary)
        and nxt is not None
        and _BARE_WORD_RE.match(nxt)
    ):
        return f"{binary} {nxt}"
    return binary


# fd-dup (`2>&1`, `1>&2`, `>&2`, `2>&-`) duplicates/closes a descriptor — it
# opens no file, so it's not a write worth gating. `&>file` is a file redirect
# (different order) and is intentionally not matched here.
_FD_DUP_RE = re.compile(r"\d*[<>]&(?:\d+|-)")
# Redirecting to /dev/null discards output — it's a sink, not a file write, so it
# doesn't warrant a prompt (`2>/dev/null`, `>/dev/null`, `&>/dev/null`, `>>/dev/null`).
# The target must end at a shell boundary, so a real path like `/dev/null2` or
# `/dev/null.txt` stays matched as a redirect (gated), not treated as the sink.
_DEVNULL_RE = re.compile(r"(?:\d*|&)>>?\s*/dev/null(?=\s|$)")


def has_redirection(segment: str) -> bool:
    """True if the segment redirects to/from a file (fd-dups like ``2>&1`` and the
    ``/dev/null`` sink don't count)."""
    cleaned = _DEVNULL_RE.sub("", _FD_DUP_RE.sub("", segment))
    return _REDIRECTION_RE.search(cleaned) is not None


def has_command_substitution(segment: str) -> bool:
    """True if the segment embeds a nested command via $(...), backticks, or <()/>().

    Quote-aware, matching what the shell actually expands: single quotes suppress
    everything (a backtick-quoted BigQuery table ref is literal, not a nested
    command), double quotes still expand ``$(…)`` and backticks but not process
    substitution, and a backslash escapes the next character. Unbalanced quoting
    is unanalyzable, so it fails closed.
    """
    i, n = 0, len(segment)
    in_single = in_double = False
    while i < n:
        ch = segment[i]
        if in_single:
            in_single = ch != "'"
            i += 1
        elif ch == "\\" and i + 1 < n:
            i += 2
        elif in_double:
            if ch == "`" or segment[i : i + 2] == "$(":
                return True
            in_double = ch != '"'
            i += 1
        elif segment[i : i + 2] == "$'":
            # ANSI-C quoting processes backslash escapes, so `$'\''` is ONE
            # literal quote. Scanning it as a plain single-quoted run desyncs
            # quote state and hides every expansion that follows.
            i += 2
            while i < n and segment[i] != "'":
                i += 2 if segment[i] == "\\" else 1
            if i >= n:
                return True
            i += 1
        elif ch == "'":
            in_single = True
            i += 1
        elif ch == '"':
            in_double = True
            i += 1
        elif ch == "`" or segment[i : i + 2] in ("$(", "<(", ">("):
            return True
        else:
            i += 1
    return in_single or in_double


__all__ = [
    "command_prefix",
    "has_command_substitution",
    "has_redirection",
    "significant_tokens",
    "split_segments",
    "strip_wrapper",
]
