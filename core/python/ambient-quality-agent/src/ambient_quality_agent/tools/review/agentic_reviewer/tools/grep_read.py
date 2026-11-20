# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Reviewer `grep` and `read` tools over the trajectory JSON.

Both tools take a path: a chain of keys, indexes and ``[*]`` steps in jq
syntax (such as ``.agents[0].instruction``), or an event ID (such as ``t0.e8``)
that can continue with such a chain. `parse_path` parses the path and
`_match_components` resolves it in Python; a ``[*]`` step expands to every
element or value. `read` returns the lines of one value, or one line per
match of a ``[*]`` path; `grep` searches all values under the path with a
regular expression, bounded by `GREP_TIMEOUT_SECONDS` of CPU time. Matching
uses the `regex` package because it supports `re` syntax while allowing
backtracking matches to time out. Both tools cap output at `TOOL_OUTPUT_CHARS` and append
an omission note when truncated.

To reduce token usage, the output format follows these rules:

1. A single-value `read` page prefixes ``N: `` only on its first line and on
   multiples of `LINE_NUMBER_INTERVAL`, because Gemini tokenizes each digit
   separately.
2. `read` formats non-string values as compact JSON split into `LINE_CHARS`-
   character lines, and omits a part's raw ``text`` when ``text_json`` is
   present (matching `grep`). Its ``fields`` argument selects keys or relative
   paths from each object value.
3. `grep` groups hits by value: the value's path once as a header, then each
   hit line as ``  N: text`` and each context line as ``  N- text``, with no
   ``--`` separators.
4. Printed paths shorten a leading ``.turns[N].events[M]`` to the event ID
   ``tN.eM``, which `parse_path` accepts.
5. `run_async` returns ``{"result": text}`` on success, which ADK passes to
   Claude as raw text.

Wiring::

    GrepTool.run_async()
        ──► GrepTool.grep() (worker thread)
              ──► search_values()
                    ──► parse_path()
                    ──► _match_components() ──► _follow_steps()
                    ──► _walk_values()
                    ──► _render_search_lines() ──► split_text_lines()
                    ──► _match_lines()
              ──► render_grep_hits() ──► render_short_path()
                                           ──► render_jq_path()
    ReadTool.run_async()
        ──► ReadTool.read() (worker thread)
              ──► _parse_fields() ──► parse_path()
              ──► parse_path()
              ──► _match_components() ──► _follow_steps()
              ──► _render_compact_value() ──► _omit_parsed_part_text()
              ──► _render_page()
                    ──► render_short_path() ──► render_jq_path()
                    ──► _render_sparse_numbered_line()
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from typing import TYPE_CHECKING, Any, Final

import regex
from google.adk.tools.base_tool import BaseTool
from google.genai import types

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from google.adk.tools.tool_context import ToolContext

GREP_TOOL = "grep"
"""Name of the search tool."""

READ_TOOL = "read"
"""Name of the read tool."""

TOOL_OUTPUT_CHARS = 30_000
"""Maximum characters returned by a single `grep` or `read` call."""

LINE_CHARS = 2_000
"""Maximum line length shown by `grep` and `read`. A longer line of a single
value is split into chunks of this length; a longer `grep` hit line or `[*]`
match line is cut."""

LINE_NUMBER_INTERVAL = 10
"""A single-value `read` page numbers its first line and every line whose
number is a multiple of this interval."""

READ_LIMIT = 400
"""Default number of lines returned by `read`."""

GREP_MAX_HITS = 100
"""Default maximum number of hits returned by `grep`."""

GREP_TIMEOUT_SECONDS = 10
"""CPU time budget, in seconds, for matching one `grep` pattern against all
searched lines.

The budget counts the CPU time of the thread that runs the search, so a search
that waits for the GIL or for a CPU while other reviews run in the same
process does not use it up."""

# Event ID shorthand: `t0.e8` expands to `.turns[0].events[8]`.
_EVENT_ID = re.compile(r"t(\d+)\.e(\d+)")

# Turn ID shorthand, accepted only as a whole path: `t3` is `.turns[3]`.
_TURN_ID = re.compile(r"t(\d+)")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_INDEX = re.compile(r"\[(-?\d+)\]")

# A string slice such as `[0:10]`, recognized only to point the model at
# `read`'s offset and limit.
_SLICE = re.compile(r"\[-?\d*:-?\d*\]")

_WILDCARD_STEP = "[*]"

# Models also write a wildcard step as `.*`, or a whole-session wildcard as `*`.
_BARE_WILDCARD = "*"

_PATH_USAGE = (
    "`path` must be a chain of keys, indexes and [*] steps in jq syntax, "
    'such as .agents[0].instruction, .turns[-1], .a["ship-to"] or '
    ".turns[0].events[*].author; an event id, such as t0.e8, optionally "
    "followed by such a chain, such as t0.e8.content.parts[*]; or "
    '"." for the whole session. Filters such as |, .[] and select() are '
    "not accepted"
)

# `json.dumps(..., ensure_ascii=False)` leaves these line breaks unescaped,
# and `str.splitlines` would split a compact JSON line at them.
_UNESCAPED_LINE_BREAKS = ("\x85", "\u2028", "\u2029")


class _Wildcard:
    """Path step that matches every element of an array or value of an object."""

    def __repr__(self) -> str:
        """Return the step as written in a path.

        Returns:
            ``[*]``.
        """
        return _WILDCARD_STEP


WILDCARD: Final = _Wildcard()
"""The ``[*]`` component that `parse_path` returns."""

PathStep = str | int | _Wildcard
"""One component of a parsed path."""

_JSON_DECODER = json.JSONDecoder()


@dataclasses.dataclass(frozen=True)
class ToolOutput:
    """What one tool call returns to the model."""

    text: str
    is_error: bool = False


_GREP_DESCRIPTION = (
    """\
Search this session with a regular expression. `path` limits the search
to one value, as in read, or to every value that a [*] path matches; "."
searches the whole session. Every string is searched line by line, and
every other value as its `"key": value` line. A text that has a parsed
`text_json` form is searched in that form only. Hits are grouped by value:
the value's path comes once, then each hit as `  <line>: <text>` and the
`context` lines before and after it as `  <line>- <text>`. A path inside an
event starts with its event id, as in:
  t0.e8.content.parts[0].text:
    4- The order has shipped.
    5: Refund refused.
read(path, line) shows the hit in its value. The output stops after
max_hits hits or """
    + f"{TOOL_OUTPUT_CHARS:,}"
    + """ characters, with a note that says how many hits
are left out.
Examples:
  grep("must|never", ".agents[0].instruction")
  grep("status", "t0.e8")
  grep("error", ".turns[*].events[*].content.parts[*].function_response")
  grep("error")"""
)

_READ_DESCRIPTION = (
    """\
Read values of this session. `path` is a path of keys and indexes in jq
syntax, such as .agents[0].instruction, .turns[-1] or .a["ship-to"]; or an
event id (t0.e8), which such a path can follow (t0.e8.content.parts[0]).
A [*] step matches every element of an array or every value of an object,
as in .turns[0].events[*].author. Filters such as |, .[] or select() do
not work.
One value comes as lines: a string as its own lines, any other value as
compact JSON. Both are cut into lines of """
    + f"{LINE_CHARS:,}"
    + f""" characters. Only a page's
first line and every {LINE_NUMBER_INTERVAL}th line ({LINE_NUMBER_INTERVAL}, \
{2 * LINE_NUMBER_INTERVAL}, ...) show their number, as
`<line>: <text>`; count from the nearest number to find a line.
A part's raw `text` is left out when its parsed `text_json` is shown; read
the `text` path to see it. A [*] path returns one line per match,
`<path>: <compact JSON>`, where a path inside an event starts with its event
id; a match over """
    + f"{LINE_CHARS:,}"
    + """ characters is cut, so read its own path to
see all of it.
`fields` keeps only the given keys (name) or relative paths
(.content.parts[0].function_call.name) of each object value.
`offset` is the first line, or match, to return; `limit` the number of
lines, or matches. The output stops after """
    + f"{TOOL_OUTPUT_CHARS:,}"
    + """ characters. A cut
output ends with a note that gives the next offset: call read again with
it to continue.
Examples:
  read(".agents[0].instruction", 1200, 200)
  read("t0.e8")
  read(".turns[0].events[*].content.parts[0].function_response", fields=["name", "response"])
  read(".turns[0].events[*]", fields=["author", ".content.parts[0].function_call.name"])"""
)


def build_grep_declaration() -> types.FunctionDeclaration:
    """Build the function declaration for `grep`.

    Returns:
        `FunctionDeclaration` with `_GREP_DESCRIPTION`, a required ``pattern``
        and optional ``path``, ``context``, ``ignore_case`` and ``max_hits``.
    """
    return types.FunctionDeclaration(
        name=GREP_TOOL,
        description=_GREP_DESCRIPTION,
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "pattern": types.Schema(
                    type=types.Type.STRING,
                    description="A regular expression (Python syntax).",
                ),
                "path": types.Schema(
                    type=types.Type.STRING, description='Default ".".'
                ),
                "context": types.Schema(
                    type=types.Type.INTEGER, description="Default 0."
                ),
                "ignore_case": types.Schema(
                    type=types.Type.BOOLEAN, description="Default true."
                ),
                "max_hits": types.Schema(
                    type=types.Type.INTEGER,
                    description=f"Default {GREP_MAX_HITS}.",
                ),
            },
            required=["pattern"],
        ),
    )


def build_read_declaration() -> types.FunctionDeclaration:
    """Build the function declaration for `read`.

    Returns:
        `FunctionDeclaration` with `_READ_DESCRIPTION`, a required ``path``
        and optional ``offset``, ``limit`` and ``fields``.
    """
    return types.FunctionDeclaration(
        name=READ_TOOL,
        description=_READ_DESCRIPTION,
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "path": types.Schema(type=types.Type.STRING),
                "offset": types.Schema(
                    type=types.Type.INTEGER, description="Default 1."
                ),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description=f"Default {READ_LIMIT}.",
                ),
                "fields": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING),
                    description="Keys or relative paths to keep from each object.",
                ),
            },
            required=["path"],
        ),
    )


def split_text_lines(text: str) -> list[tuple[int, str]]:
    """Split a string into numbered lines for `grep` and `read`.

    Splits on `str.splitlines` boundaries and subdivides lines longer than
    `LINE_CHARS` so every character is addressable by line number.

    Args:
        text: Input string.

    Returns:
        ``(char_offset, line_text)`` pairs, or ``[(0, "")]`` when `text` is
        empty.
    """
    lines: list[tuple[int, str]] = []
    offset = 0
    for physical in text.splitlines(keepends=True):
        body = physical.splitlines()[0]
        for start in range(0, max(len(body), 1), LINE_CHARS):
            lines.append((offset + start, body[start : start + LINE_CHARS]))
        offset += len(physical)
    return lines or [(0, "")]


def render_jq_path(components: Sequence[str | int]) -> str:
    """Format path components as a jq path expression.

    Args:
        components: Sequence of object keys and array indices from the root.

    Returns:
        jq path expression (e.g., ``.agents[0].instruction`` or ``.a["b c"]``),
        or ``.`` for the root.
    """
    rendered = ""
    for component in components:
        if isinstance(component, int):
            rendered += f"[{component}]"
        elif _IDENTIFIER.fullmatch(component):
            rendered += f".{component}"
        else:
            rendered += f"[{json.dumps(component, ensure_ascii=False)}]"
    if not rendered:
        return "."
    return rendered if rendered.startswith(".") else "." + rendered


def render_short_path(components: Sequence[str | int]) -> str:
    """Format path components with a leading event ID shorthand when applicable.

    Args:
        components: Canonical object keys and array indexes from the root.

    Returns:
        ``tN.eM`` followed by the jq path of the remaining steps when
        `components` starts with ``"turns", N, "events", M`` for non-negative
        ``N`` and ``M`` (e.g., ``t0.e8.content`` or ``t0.e8["key with space"]``);
        otherwise `render_jq_path(components)`. `parse_path` parses both forms
        back to `components`.
    """
    if (
        len(components) >= 4
        and components[0] == "turns"
        and components[2] == "events"
        and isinstance(turn := components[1], int)
        and isinstance(event := components[3], int)
        and turn >= 0
        and event >= 0
    ):
        rest = render_jq_path(components[4:])
        if rest == ".":
            rest = ""
        elif rest.startswith(".["):
            rest = rest[1:]
        return f"t{turn}.e{event}{rest}"
    return render_jq_path(components)


def parse_path(path: str) -> list[PathStep] | ToolOutput:
    """Parse a `grep` or `read` path into object keys and array indexes.

    The path is ``.`` for the whole document, or a chain of steps that starts
    with ``.`` or with an event ID such as ``t0.e8``. A step is ``.name``,
    ``."key"``, ``.[N]``, ``[N]``, ``.["key"]``, ``["key"]``, ``[*]`` or
    ``.[*]``, where ``N`` may be negative. These are the forms `render_jq_path` writes, so a path
    that `grep` prints parses back to the same components.

    Models also write a few shapes that are not jq paths, and the parser
    accepts them because their meaning is unambiguous:

    1. A bare turn ID, such as ``t3``, as the whole path means ``.turns[3]``.
    2. ``.*`` as a step means ``[*]``, and ``*`` as the whole path means
       ``.[*]``.
    3. A path that starts with ``[*]`` without a leading dot means ``.[*]``
       followed by the rest of the path.

    Args:
        path: Path written by the model; surrounding whitespace is ignored and
            an empty path means ``.``.

    Returns:
        Keys, indexes and `WILDCARD` steps from the root, with an event ID
        expanded to ``"turns", N, "events", M``, or an error `ToolOutput`
        that names the accepted forms.
    """
    spec = path.strip() or "."
    invalid = ToolOutput(
        f"{path!r} is not a path that this tool accepts\n{_PATH_USAGE}",
        is_error=True,
    )
    components: list[PathStep] = []
    position = 0
    if turn := _TURN_ID.fullmatch(spec):
        return ["turns", int(turn[1])]
    if spec == _BARE_WILDCARD:
        return [WILDCARD]
    if event := _EVENT_ID.match(spec):
        components = ["turns", int(event[1]), "events", int(event[2])]
        position = event.end()
    elif spec == ".":
        return components
    elif not spec.startswith((".", _WILDCARD_STEP)):
        return invalid
    while position < len(spec):
        dotted = spec[position] == "."
        start = position + dotted
        if dotted and (step := _IDENTIFIER.match(spec, start)):
            components.append(step[0])
            position = step.end()
        elif spec.startswith(_WILDCARD_STEP, start):
            components.append(WILDCARD)
            position = start + len(_WILDCARD_STEP)
        elif dotted and spec.startswith(_BARE_WILDCARD, start):
            components.append(WILDCARD)
            position = start + len(_BARE_WILDCARD)
        elif step := _INDEX.match(spec, start):
            components.append(int(step[1]))
            position = step.end()
        elif _SLICE.match(spec, start):
            return ToolOutput(
                f"{path!r} slices a value; give the whole value's path and "
                "use read's offset and limit instead",
                is_error=True,
            )
        else:
            bracketed = spec.startswith("[", start)
            if not (dotted or bracketed):
                return invalid
            try:
                key, position = _JSON_DECODER.raw_decode(
                    spec, start + bracketed
                )
            except json.JSONDecodeError:
                return invalid
            if not isinstance(key, str) or (
                bracketed and not spec.startswith("]", position)
            ):
                return invalid
            components.append(key)
            position += bracketed
    return components


def _match_components(
    trajectory_json: dict[str, Any], components: Sequence[PathStep], path: str
) -> list[tuple[list[str | int], Any]] | ToolOutput:
    """Expand parsed path components to every value they match.

    Args:
        trajectory_json: Trajectory JSON to walk.
        components: Output of `parse_path`.
        path: The path as written, for the error message.

    Returns:
        ``(canonical_components, value)`` pairs in document order, or an error
        `ToolOutput` if no value matches.
    """
    matches: list[tuple[list[str | int], Any]] = [([], trajectory_json)]
    for component in components:
        expanded: list[tuple[list[str | int], Any]] = []
        for canonical, value in matches:
            if isinstance(component, _Wildcard):
                if isinstance(value, dict):
                    expanded += [
                        ([*canonical, str(key)], item)
                        for key, item in value.items()
                    ]
                elif isinstance(value, list):
                    expanded += [
                        ([*canonical, index], item)
                        for index, item in enumerate(value)
                    ]
            elif step := _follow_steps(value, [component]):
                expanded.append(([*canonical, *step[0]], step[1]))
        matches = expanded
    if not matches:
        return ToolOutput(
            f"{path!r} selects no value in this session", is_error=True
        )
    return matches


def _follow_steps(
    value: Any, components: Sequence[PathStep]
) -> tuple[list[str | int], Any] | None:
    """Follow keys and indexes from `value`.

    Args:
        value: Value to start from.
        components: Keys and indexes; a negative index counts from the end.

    Returns:
        ``(canonical_components, value)`` with negative indexes replaced by
        their position, or None if a step does not exist or is a wildcard.
    """
    canonical: list[str | int] = []
    for component in components:
        if isinstance(component, str) and isinstance(value, dict):
            if component not in value:
                return None
            value = value[component]
            canonical.append(component)
        elif isinstance(component, int) and isinstance(value, list):
            if not -len(value) <= component < len(value):
                return None
            index = component % len(value)
            value = value[index]
            canonical.append(index)
        else:
            return None
    return canonical, value


@dataclasses.dataclass(frozen=True)
class SearchHits:
    """The values that one `grep` call searched and its hits in them."""

    values: list[tuple[list[str | int], list[tuple[str, ...]]]]
    """Canonical path components and candidate search lines of each value
    with at least one line, in document order."""

    hits: list[tuple[int, list[int]]]
    """``(value_index, line_indices)`` pairs for each value with a hit, where
    ``value_index`` indexes `values`."""


def search_values(
    trajectory_json: dict[str, Any],
    pattern: str,
    path: str,
    *,
    ignore_case: bool,
) -> SearchHits | ToolOutput:
    """Search every value under a path with a regular expression.

    Strings are searched line by line; the first line of an object member
    is also tested as ``"key": line`` so key names match their values.
    Other leaves are searched as ``"key": <json>`` (or ``<json>`` for array
    items), and non-empty object or array members as ``"key": {`` or
    ``"key": [``. A part's raw ``text`` is skipped when ``text_json`` is
    present.

    Args:
        trajectory_json: Trajectory JSON built by
            `trajectory_json.build_trajectory_json`.
        pattern: Python regular expression.
        path: Path in a form that `parse_path` accepts; ``.`` searches the
            whole trajectory JSON, and ``[*]`` steps search under every
            match.
        ignore_case: Whether to match case-insensitively.

    Returns:
        The searched values and their hits, or an error `ToolOutput` if
        `pattern` or `path` is invalid or matching times out.
    """
    # Validate with `re` first because the tool declaration specifies
    # Python `re` syntax; `_match_lines` then executes the match with `regex`.
    try:
        re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return ToolOutput(
            f"invalid regular expression {pattern!r}: {exc}", is_error=True
        )
    components = parse_path(path)
    if isinstance(components, ToolOutput):
        return components
    roots = _match_components(trajectory_json, components, path)
    if isinstance(roots, ToolOutput):
        return roots
    searched = [
        (
            parts,
            _render_search_lines(item, parts, is_member=len(parts) > len(base)),
        )
        for base, value in roots
        for parts, item in _walk_values(value, list(base))
    ]
    searched = [(parts, lines) for parts, lines in searched if lines]
    matched = _match_lines(
        pattern, ignore_case, [lines for _, lines in searched]
    )
    if isinstance(matched, ToolOutput):
        return matched
    return SearchHits(values=searched, hits=matched)


def render_grep_hits(
    searched: SearchHits, *, context: int, max_hits: int
) -> ToolOutput:
    """Render the kept hits of a search and their context lines.

    Each value's first shown line follows a ``<path>:`` header. A hit is
    ``  N: text`` and a context line ``  N- text``. A value's header and
    line are kept or cut whole at the `TOOL_OUTPUT_CHARS` budget, so the
    output never ends with a header alone.

    Args:
        searched: Output of `search_values`.
        context: Number of surrounding lines from the same value to include
            around each hit; values below 0 count as 0.
        max_hits: Maximum number of hits to return; values below 1 count as
            1. A left-out hit within the after-context of the last kept hit
            shows as a context line.

    Returns:
        The rendered lines followed by an omission note if truncated, or
        ``(no matches)``.
    """
    context = max(0, context)
    max_hits = max(1, max_hits)
    out: list[str] = []
    chars = 0
    shown = 0
    left_out = 0
    stop = ""
    for value_index, hits in searched.hits:
        # Choose the kept hits before rendering, so the output ends with
        # the last kept hit and its after-context, never with context
        # lines that lead up to a hit that is left out.
        kept = [] if stop else hits[: max_hits - shown]
        left_out += len(hits) - len(kept)
        if not kept:
            stop = stop or "max_hits"
            continue
        components, lines = searched.values[value_index]
        kept_set = set(kept)
        window = sorted(
            {
                index
                for hit in kept
                for index in range(
                    max(0, hit - context),
                    min(len(lines), hit + context + 1),
                )
            }
        )
        has_header = False
        for index in window:
            is_hit = index in kept_set
            mark = ":" if is_hit else "-"
            block = [f"  {index + 1}{mark} {_cut_line(lines[index][-1])}"]
            if not has_header:
                block.insert(0, f"{render_short_path(components)}:")
            size = sum(len(text) + 1 for text in block)
            if out and chars + size > TOOL_OUTPUT_CHARS:
                stop = "chars"
                left_out += sum(1 for hit in kept if hit >= index)
                break
            out += block
            chars += size
            shown += int(is_hit)
            has_header = True
        if not stop and len(kept) < len(hits):
            stop = "max_hits"
    if not out:
        return ToolOutput("(no matches)")
    if stop == "max_hits":
        out.append(
            f"[stopped at max_hits={max_hits}: {left_out} more hits left "
            "out; narrow the pattern or the path, or raise max_hits]"
        )
    elif stop == "chars":
        out.append(
            f"[output stopped at {TOOL_OUTPUT_CHARS} chars: {left_out} "
            "more hits left out; narrow the pattern or the path]"
        )
    return ToolOutput("\n".join(out))


class GrepTool(BaseTool):
    """ADK `grep` tool bound to a single trajectory JSON."""

    def __init__(self, trajectory_json: dict[str, Any]) -> None:
        """Initialize the tool.

        Args:
            trajectory_json: Trajectory JSON built by
                `trajectory_json.build_trajectory_json`. The tool only reads
                it, so concurrent calls can share it.
        """
        self._declaration = build_grep_declaration()
        super().__init__(
            name=GREP_TOOL, description=self._declaration.description or ""
        )
        self._trajectory_json = trajectory_json

    def _get_declaration(self) -> types.FunctionDeclaration:
        """Return the tool's function declaration.

        Returns:
            `grep` function declaration.
        """
        return self._declaration

    def grep(
        self,
        pattern: str,
        path: str = ".",
        *,
        context: int = 0,
        ignore_case: bool = True,
        max_hits: int = GREP_MAX_HITS,
    ) -> ToolOutput:
        """Search values under `path` with a regular expression.

        Args:
            pattern: Python regular expression.
            path: Path in a form that `parse_path` accepts, with ``[*]`` steps;
                ``.`` searches the whole trajectory JSON.
            context: Number of surrounding lines from the same value to include
                around each hit.
            ignore_case: Whether to match case-insensitively.
            max_hits: Maximum number of hits to return.

        Returns:
            Per value with hits, a ``<path>:`` header followed by ``  N: text``
            hit lines and ``  N- text`` context lines, then an omission note
            if truncated; ``(no matches)``; or an error `ToolOutput` if
            `pattern` or `path` is invalid.
        """
        searched = search_values(
            self._trajectory_json, pattern, path, ignore_case=ignore_case
        )
        if isinstance(searched, ToolOutput):
            return searched
        return render_grep_hits(searched, context=context, max_hits=max_hits)

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, str]:
        """Run a `grep` call on a worker thread.

        Offloads `grep` to a thread so parallel tool calls within a single
        model reply can overlap.

        Args:
            args: Tool call arguments: ``pattern``, and optionally ``path``,
                ``context``, ``ignore_case`` and ``max_hits``.
            tool_context: ADK tool execution context (unused).

        Returns:
            ``{"result": text}`` on success or ``{"error": text}`` for an
            invalid argument, pattern or path.
        """
        del tool_context
        try:
            context = coerce_int(args.get("context"), 0)
            max_hits = coerce_int(args.get("max_hits"), GREP_MAX_HITS)
        except (TypeError, ValueError) as exc:
            return {"error": f"invalid arguments for {GREP_TOOL}: {exc}"}
        output = await asyncio.to_thread(
            self.grep,
            str(args.get("pattern") or ""),
            str(args.get("path") or "."),
            context=context,
            ignore_case=coerce_bool(args.get("ignore_case"), True),
            max_hits=max_hits,
        )
        # ADK's Anthropic adapter passes exactly {"result": <str>} to Claude as
        # raw text, and JSON-encodes any other dict, which escapes quotes, line
        # breaks and non-ASCII text into more tokens.
        return {"error" if output.is_error else "result": output.text}


class ReadTool(BaseTool):
    """ADK `read` tool bound to a single trajectory JSON."""

    def __init__(self, trajectory_json: dict[str, Any]) -> None:
        """Initialize the tool.

        Args:
            trajectory_json: Trajectory JSON built by
                `trajectory_json.build_trajectory_json`. The tool only reads
                it, so concurrent calls can share it.
        """
        self._declaration = build_read_declaration()
        super().__init__(
            name=READ_TOOL, description=self._declaration.description or ""
        )
        self._trajectory_json = trajectory_json

    def _get_declaration(self) -> types.FunctionDeclaration:
        """Return the tool's function declaration.

        Returns:
            `read` function declaration.
        """
        return self._declaration

    def read(
        self,
        path: str,
        offset: int = 1,
        limit: int = READ_LIMIT,
        *,
        fields: Sequence[str] | None = None,
    ) -> ToolOutput:
        """Read trajectory JSON values.

        One value is a string's own lines, or any other value as compact JSON,
        cut into lines of `LINE_CHARS` characters. A ``[*]`` path gives one
        ``<path>: <compact JSON>`` line per match, cut at `LINE_CHARS`.

        Args:
            path: Path in a form that `parse_path` accepts, with ``[*]`` steps.
            offset: 1-based starting line number, or match number for a
                ``[*]`` path.
            limit: Maximum number of lines, or matches, to return.
            fields: Keys, or relative paths that start with ``.``, to keep
                from each object value; None or empty keeps whole values.

        Returns:
            The lines of one value, where only the first line and every
            `LINE_NUMBER_INTERVAL`-th line carry an ``N: `` prefix, or
            ``<path>: <compact JSON>`` lines for the matches of a ``[*]``
            path, capped at `TOOL_OUTPUT_CHARS` (with a continuation note when
            lines remain), or an error `ToolOutput`.
        """
        selected = _parse_fields(fields or ())
        if isinstance(selected, ToolOutput):
            return selected
        components = parse_path(path)
        if isinstance(components, ToolOutput):
            return components
        matches = _match_components(self._trajectory_json, components, path)
        if isinstance(matches, ToolOutput):
            return matches
        if WILDCARD in components:
            return _render_page(
                len(matches),
                lambda number: _cut_line(
                    f"{render_short_path(matches[number - 1][0])}: "
                    + _render_compact_value(*matches[number - 1], selected)
                ),
                offset,
                limit,
                unit="matches",
                owner="path",
            )
        canonical, value = matches[0]
        text = (
            value
            if isinstance(value, str)
            else _render_compact_value(canonical, value, selected)
        )
        lines = [line for _, line in split_text_lines(text)]
        first = max(1, offset)
        return _render_page(
            len(lines),
            lambda number: _render_sparse_numbered_line(
                number, lines[number - 1], first
            ),
            first,
            limit,
            unit="lines",
            owner="value",
        )

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, str]:
        """Run a `read` call on a worker thread.

        Offloads `read` to a thread so parallel tool calls within a single
        model reply can overlap.

        Args:
            args: Tool call arguments: ``path``, and optionally ``offset``,
                ``limit`` and ``fields``.
            tool_context: ADK tool execution context (unused).

        Returns:
            ``{"result": text}`` on success or ``{"error": text}`` for an
            invalid argument, path or offset.
        """
        del tool_context
        try:
            offset = coerce_int(args.get("offset"), 1)
            limit = coerce_int(args.get("limit"), READ_LIMIT)
            fields = coerce_str_list(args.get("fields"))
        except (TypeError, ValueError) as exc:
            return {"error": f"invalid arguments for {READ_TOOL}: {exc}"}
        output = await asyncio.to_thread(
            self.read,
            str(args.get("path") or ""),
            offset,
            limit,
            fields=fields,
        )
        # Exactly {"result": <str>} reaches Claude as raw text; see
        # `GrepTool.run_async`.
        return {"error" if output.is_error else "result": output.text}


def _render_sparse_numbered_line(number: int, text: str, first: int) -> str:
    """Render one line of a single-value `read` page.

    Args:
        number: 1-based line number.
        text: Line text.
        first: Number of the page's first line.

    Returns:
        ``N: text`` for the page's first line and for a line whose number is
        a multiple of `LINE_NUMBER_INTERVAL`, otherwise `text` alone.
    """
    if number == first or number % LINE_NUMBER_INTERVAL == 0:
        return f"{number}: {text}"
    return text


def _render_page(
    total: int,
    render_item: Callable[[int], str],
    offset: int,
    limit: int,
    *,
    unit: str,
    owner: str,
) -> ToolOutput:
    """Render one page of `read` output.

    Args:
        total: Number of items (lines or matches) available.
        render_item: Renders the item with the given 1-based number as one
            output line.
        offset: 1-based first item to return; values below 1 count as 1.
        limit: Maximum number of items to return; values below 1 count as 1.
        unit: Plural item name for the notes, such as ``lines``.
        owner: What holds the items, for the past-the-end error, such as
            ``value``.

    Returns:
        The rendered items capped at `TOOL_OUTPUT_CHARS`, with a continuation
        note when items remain, or an error `ToolOutput` if `offset` is past
        the end.
    """
    offset = max(1, offset)
    limit = max(1, limit)
    if offset > total:
        return ToolOutput(
            f"offset {offset} is past the end: the {owner} has {total} {unit}",
            is_error=True,
        )
    out: list[str] = []
    chars = 0
    number = offset
    while number <= min(total, offset + limit - 1):
        line = render_item(number)
        if out and chars + len(line) + 1 > TOOL_OUTPUT_CHARS:
            break
        out.append(line)
        chars += len(line) + 1
        number += 1
    if number <= total:
        out.append(
            f"[returned {unit} {offset}-{number - 1} of {total}; continue "
            f"with offset {number}]"
        )
    return ToolOutput("\n".join(out))


def _parse_fields(
    fields: Sequence[str],
) -> list[tuple[str, list[PathStep]]] | ToolOutput:
    """Parse `read`'s ``fields`` into the steps each field selects.

    Args:
        fields: Keys, or relative paths that start with ``.``.

    Returns:
        ``(field, steps)`` pairs in the given order, or an error `ToolOutput`
        for a relative path that does not parse or holds a ``[*]`` step.
    """
    parsed: list[tuple[str, list[PathStep]]] = []
    for field in fields:
        if not field.startswith("."):
            parsed.append((field, [field]))
            continue
        steps = parse_path(field)
        if isinstance(steps, ToolOutput) or WILDCARD in steps:
            return ToolOutput(
                f"field {field!r} is neither a key nor a relative path such "
                'as ".content.parts[0].function_call.name"',
                is_error=True,
            )
        parsed.append((field, steps))
    return parsed


def _render_compact_value(
    components: Sequence[str | int],
    value: Any,
    fields: Sequence[tuple[str, Sequence[PathStep]]],
) -> str:
    """Render a value as one line of compact JSON for `read`.

    Args:
        components: Canonical path components of `value`.
        value: Trajectory JSON value.
        fields: Output of `_parse_fields`; when not empty, an object value is
            replaced by an object of the fields it has, keyed by field.

    Returns:
        Compact JSON without line breaks, where a part's raw ``text`` beside
        its ``text_json`` is left out.
    """
    if fields and isinstance(value, dict):
        selected: dict[str, Any] = {}
        for field, steps in fields:
            if found := _follow_steps(value, steps):
                selected[field] = _omit_parsed_part_text(
                    found[1], [*components, *found[0]]
                )
        shown: Any = selected
    else:
        shown = _omit_parsed_part_text(value, components)
    text = json.dumps(
        shown, ensure_ascii=False, separators=(",", ":"), default=str
    )
    for line_break in _UNESCAPED_LINE_BREAKS:
        text = text.replace(line_break, f"\\u{ord(line_break):04x}")
    return text


def _is_parsed_part(
    value: dict[str, Any], components: Sequence[str | int]
) -> bool:
    """Check whether `value` is a content part with a parsed ``text_json``.

    Args:
        value: Trajectory JSON object.
        components: Canonical path components of `value`.

    Returns:
        True if `value` has ``text_json`` and is an element of a ``parts``
        array, meaning its raw ``text`` duplicates the parsed payload.
    """
    return (
        "text_json" in value
        and len(components) >= 2
        and components[-2] == "parts"
    )


def _omit_parsed_part_text(value: Any, components: Sequence[str | int]) -> Any:
    """Return `value` without the raw ``text`` of parts that have ``text_json``.

    Matches `_walk_values` so `read` and `grep` expose a JSON payload in one
    form only.

    Args:
        value: Trajectory JSON value.
        components: Canonical path components of `value`.

    Returns:
        `value`, or a copy with those ``text`` keys omitted.
    """
    if isinstance(value, dict):
        is_parsed_part = _is_parsed_part(value, components)
        return {
            key: _omit_parsed_part_text(item, [*components, str(key)])
            for key, item in value.items()
            if not (is_parsed_part and key == "text")
        }
    if isinstance(value, list):
        return [
            _omit_parsed_part_text(item, [*components, index])
            for index, item in enumerate(value)
        ]
    return value


def _cut_line(line: str) -> str:
    """Truncate a rendered line exceeding `LINE_CHARS`.

    Args:
        line: Rendered line text.

    Returns:
        `line` unchanged if within `LINE_CHARS`, otherwise its first
        `LINE_CHARS` characters followed by a truncation notice.
    """
    if len(line) <= LINE_CHARS:
        return line
    return (
        f"{line[:LINE_CHARS]}… [line cut at {LINE_CHARS} of {len(line)} chars; "
        "read this value's own path to see all of it]"
    )


class _CpuTimeBudget:
    """CPU time budget of the calling thread for one `grep` search.

    Reading the thread's CPU clock costs about as much as one short regex
    search, so the budget estimates the CPU time left from the wall clock and
    reads the CPU clock only when that estimate runs out. A thread's CPU time
    grows no faster than wall-clock time, so the estimate never exceeds the CPU
    time that is really left.
    """

    def __init__(self, seconds: float) -> None:
        """Start the budget at the calling thread's current CPU time.

        Args:
            seconds: CPU seconds the search may use.
        """
        self._cpu_end = time.thread_time() + seconds
        self._cpu_left = seconds
        self._checked_at = time.monotonic()

    def compute_seconds_left(self) -> float:
        """Compute a lower bound of the CPU seconds left in the budget.

        Returns:
            The CPU seconds left, or a value at or below zero when the budget
            is used up.
        """
        left = self._cpu_left - (time.monotonic() - self._checked_at)
        if left > 0:
            return left
        self._cpu_left = self._cpu_end - time.thread_time()
        self._checked_at = time.monotonic()
        return self._cpu_left

    def discard_estimate(self) -> None:
        """Make the next `compute_seconds_left` call read the CPU clock."""
        self._cpu_left = 0.0


def _match_lines(
    pattern: str,
    ignore_case: bool,
    values: Sequence[Sequence[tuple[str, ...]]],
) -> list[tuple[int, list[int]]] | ToolOutput:
    """Match a pattern against rendered lines within `GREP_TIMEOUT_SECONDS`.

    The budget counts the CPU time of the calling thread, so time spent waiting
    for the GIL or for a CPU does not count against it.

    Args:
        pattern: Regular expression pre-validated by `re.compile`.
        ignore_case: Whether to match case-insensitively.
        values: Rendered lines per value, where each line is a tuple of
            candidate strings to test against `pattern`.

    Returns:
        ``(value_index, line_indices)`` pairs for each matching value in order,
        or an error `ToolOutput` if `regex` rejects `pattern` or matching
        uses more than `GREP_TIMEOUT_SECONDS` of CPU time.
    """
    try:
        compiled = regex.compile(
            pattern, regex.IGNORECASE if ignore_case else 0
        )
    except regex.error as exc:
        return ToolOutput(
            f"invalid regular expression {pattern!r}: {exc}", is_error=True
        )
    budget = _CpuTimeBudget(GREP_TIMEOUT_SECONDS)

    def matches(form: str) -> bool:
        """Check whether `pattern` matches one candidate string.

        Args:
            form: Candidate string.

        Returns:
            True if `pattern` matches somewhere in `form`.

        Raises:
            TimeoutError: If the CPU time budget is used up.
        """
        while (left := budget.compute_seconds_left()) > 0:
            try:
                # `concurrent=True` releases the GIL so parallel tool calls
                # can proceed while a pattern backtracks. The `regex` timeout
                # is wall-clock time, which is never shorter than the CPU time
                # the call uses.
                return (
                    compiled.search(form, concurrent=True, timeout=left)
                    is not None
                )
            except TimeoutError:
                # The call used up `left` seconds of wall-clock time, but
                # possibly less CPU time when the CPUs are busy; retry only
                # while CPU time is left.
                budget.discard_estimate()
        raise TimeoutError

    hits = []
    try:
        for value_index, lines in enumerate(values):
            matched = [
                line_index
                for line_index, forms in enumerate(lines)
                if any(matches(form) for form in forms)
            ]
            if matched:
                hits.append((value_index, matched))
    except TimeoutError:
        return ToolOutput(
            f"the pattern used more than {GREP_TIMEOUT_SECONDS} s of CPU time "
            "and was stopped; simplify it, for example by removing nested "
            "repetition",
            is_error=True,
        )
    return hits


def _walk_values(
    value: Any, components: list[str | int]
) -> Iterator[tuple[list[str | int], Any]]:
    """Yield `value` and all nested values with their paths in JSON order.

    Args:
        value: Root value to traverse.
        components: Path components to `value`.

    Yields:
        ``(path_components, value)`` pairs in pre-order. When a content part
        contains ``text_json``, its raw ``text`` field is skipped so JSON
        payloads are searched only once.
    """
    yield components, value
    if isinstance(value, dict):
        is_parsed_part = _is_parsed_part(value, components)
        for key, item in value.items():
            if is_parsed_part and key == "text":
                continue
            yield from _walk_values(item, [*components, str(key)])
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_values(item, [*components, index])


def _render_search_lines(
    value: Any, components: Sequence[str | int], *, is_member: bool
) -> list[tuple[str, ...]]:
    """Render candidate search lines for a single value in `grep`.

    Args:
        value: Trajectory JSON value to render.
        components: Path components to `value`.
        is_member: True if `value` is nested inside the search root (so its
            object key prefix is included); False for the search root itself.

    Returns:
        One tuple of candidate strings per line; the last element is the text
        displayed on a hit. Non-empty dicts and lists emit a single
        ``"key": {`` or ``"key": [`` line when they are object members, and no
        lines otherwise (their elements are searched individually).
    """
    key = components[-1] if is_member and components else None
    prefix = (
        f"{json.dumps(key, ensure_ascii=False)}: "
        if isinstance(key, str)
        else ""
    )
    if isinstance(value, (dict, list)) and value:
        if not prefix:
            return []
        return [(prefix + ("{" if isinstance(value, dict) else "["),)]
    if isinstance(value, str):
        lines: list[tuple[str, ...]] = [
            (line,) for _, line in split_text_lines(value)
        ]
        if prefix:
            lines[0] = (lines[0][0], prefix + lines[0][0])
        return lines
    return [(prefix + json.dumps(value, ensure_ascii=False, default=str),)]


def coerce_int(value: Any, default: int) -> int:
    """Coerce a tool argument (possibly a whole-number float) to `int`.

    Args:
        value: Raw argument value, or None if omitted.
        default: Default returned when `value` is None.

    Returns:
        Coerced integer.

    Raises:
        ValueError: If `value` is a boolean or not a whole number.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")
    number = float(value)
    if not number.is_integer():
        raise ValueError(f"expected an integer, got {value!r}")
    return int(number)


def coerce_str_list(value: Any) -> list[str] | None:
    """Coerce a tool argument to a list of strings.

    Args:
        value: Raw argument value: None if omitted, one string, or a list of
            strings.

    Returns:
        The strings, or None when `value` is None.

    Raises:
        ValueError: If `value` is neither a string nor a list of strings.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        return list(value)
    raise ValueError(f"expected an array of strings, got {value!r}")


def coerce_bool(value: Any, default: bool) -> bool:
    """Coerce a tool argument (possibly a string) to `bool`.

    Args:
        value: Raw argument value, or None/empty if omitted.
        default: Default returned when `value` is None or ``""``.

    Returns:
        Coerced boolean.
    """
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no")
    return bool(value)
