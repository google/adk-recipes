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

"""Reviewer `query_trajectory` jq tool over the trajectory JSON.

Each call runs in `_jq_worker.py`, a child process terminated after
`QUERY_TIMEOUT_SECONDS`. At most `WORKER_CONCURRENCY` workers run concurrently
across all reviews in this process because each worker can allocate up to its
address-space limit. Output exceeding `QUERY_OUTPUT_CHARS` is truncated with a
note containing the exact expression to fetch the next page.

Wiring::

    JqQueryTool.run_async()
        ──► JqQueryTool.query_trajectory() (worker thread)
              ──► run_jq() ──► _jq_worker.py (child process)
              ──► render_truncation_note() (truncated output only)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any, Final, Literal

from ambient_quality_agent.tools.review.agentic_reviewer.tools import _jq_worker
from google.adk.tools.base_tool import BaseTool
from google.genai import types

if TYPE_CHECKING:
    from google.adk.tools.tool_context import ToolContext

QUERY_TOOL = "query_trajectory"
"""Name of the jq tool."""

QUERY_OUTPUT_CHARS = 30_000
"""Maximum characters returned by a single `query_trajectory` call."""

QUERY_COUNT_CHARS = 1_000_000
"""Output size past which a truncated query stops counting the total."""

QUERY_COUNT_SECONDS = 5
"""Time after which a truncated query stops counting the total.

Below `QUERY_TIMEOUT_SECONDS`, so counting does not discard the text already
returned."""

QUERY_TIMEOUT_SECONDS = 10
"""Wall-clock limit for one jq expression."""

WORKER_CONCURRENCY = 4
"""Maximum concurrent jq worker processes per Python process.

Tool calls within a single model reply run concurrently, and the review node
runs multiple session reviews in parallel. Because each worker can allocate up
to `_jq_worker.MEMORY_LIMIT_BYTES`, this limit caps total worker memory per
process at 4 GiB (half the engine's default memory) and matches its default 4
vCPUs. Each process in a multi-process container enforces its own limit."""

_WORKER_SLOTS = threading.BoundedSemaphore(WORKER_CONCURRENCY)

_SURROGATE = re.compile("[\ud800-\udfff]")

_JQ_WORKER = pathlib.Path(__file__).with_name("_jq_worker.py")

# Shared-library Python builds without an RPATH, such as the setup-python
# interpreter in CI, find libpython only through `LD_LIBRARY_PATH`.
_WORKER_ENV_VARIABLES = ("LD_LIBRARY_PATH",)

# A plain path such as `.agents[2].instruction`, optionally ending in a string
# slice `[start:end]`. Such a path takes a slice without parentheses, and the
# truncation note reads absolute offsets back from a slice the model wrote.
_SLICEABLE_PATH = re.compile(
    r"""\s*(?P<base>(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\[\d+\]|\.\["[^"\\]*"\])"""
    r"""(?:\.[A-Za-z_][A-Za-z0-9_]*|\.?\[\d+\]|\.?\["[^"\\]*"\])*)"""
    r"""(?:\[(?P<start>\d*):(?P<end>\d*)\])?\s*"""
)

_QUERY_DESCRIPTION = (
    """\
A standard jq tool. Its input `.` is one JSON document: this session, as
the first message shows it. Every {"$elided": ...} value is there in full.
Any jq expression works. A reminder of the built-ins:
  access       .a.b  .a[2]  .a[-1]  .a[1:3]  .s[100:200]  .a[]  ..  .a?
  filter       select(f)  map(f)  first  last  limit(n; f)  range(n)
  compare      ==  !=  <  <=  >  >=  and  or  not
  branch       if c then a elif c then b else d end   a // b   try f catch g
  collections  keys  has(k)  length  add  any  all  min  max  unique  sort
               sort_by(f)  group_by(f)  min_by(f)  max_by(f)  to_entries
               from_entries  with_entries(f)  index  indices  contains
               inside  flatten  reverse  del(f)  pick(f)
  strings      test  match  capture  scan  splits  sub  gsub  split  join
               ascii_downcase  ascii_upcase  ltrimstr  rtrimstr  trim
               startswith  endswith  tostring  tonumber
  paths        paths  paths(f)  path(f)  getpath  setpath  tostream
  JSON text    tojson  fromjson
  variables    f as $x | g   reduce   foreach   def name: f;
Each result comes on its own line: a string as raw text, any other value
as compact JSON.
"""
    + f"Output stops after {QUERY_OUTPUT_CHARS:,} characters."
    + """ A cut output ends with a
[truncated: ...] note that gives the exact expression for the next page:
run it to continue. This way you can read every value to its end.
Positions in a long text are character offsets: match returns them, and
a string slice takes them.
Examples:
  .agents | map(.agent_id)
  .turns[0].events[4:9]
  [.turns[].events[] | select(.content.parts[0].function_call) | .event_id]
  .agents[0].instruction[12000:18000]
  .agents[0].instruction | scan(".*must.*")
      each matching line ("." does not match a newline)
  .agents[0].instruction | match(".*refund.*"; "g") | {offset, line: .string}
      each matching line with its offset, to read around it with a slice
  .turns[0].events[8].content.parts[0].function_response.response
    | tostream | select(length == 2) | select(tojson | test("status"; "i"))
      each matching value with its path; without the last select, it
      lists every value of a large object one by one
  [paths(strings) as $p | select(getpath($p) | test("error"; "i")) | $p]
      the path of every string in the document that matches"""
)


@dataclasses.dataclass(frozen=True)
class ToolOutput:
    """What one tool call returns to the model."""

    text: str
    is_error: bool = False


_STRING_PAGE: Final = "string"
_RESULTS_PAGE: Final = "results"


@dataclasses.dataclass(frozen=True)
class PageOrigin:
    """Base expression and absolute offset for a pagination continuation.

    Subsequent truncation notes are built from `base` rather than nesting the
    previous continuation expression, keeping continuation expressions flat and
    offsets absolute.
    """

    kind: Literal["string", "results"]
    """`_STRING_PAGE` for character slices of a single string result;
    `_RESULTS_PAGE` for slices of a result stream."""

    base: str
    """Underlying jq expression being paginated."""

    offset: int
    """Absolute character or result index where the page starts in `base`."""


@dataclasses.dataclass(frozen=True)
class TruncationNote:
    """Truncation note text and the continuation expressions it offers."""

    text: str
    continuations: dict[str, PageOrigin]
    """Mapping from each suggested continuation expression to its `PageOrigin`."""


def build_query_declaration() -> types.FunctionDeclaration:
    """Build the function declaration for `query_trajectory`.

    Returns:
        `FunctionDeclaration` with `_QUERY_DESCRIPTION` and a single required
        string parameter ``expr``.
    """
    return types.FunctionDeclaration(
        name=QUERY_TOOL,
        description=_QUERY_DESCRIPTION,
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "expr": types.Schema(
                    type=types.Type.STRING, description="A jq expression."
                )
            },
            required=["expr"],
        ),
    )


class JqQueryTool(BaseTool):
    """ADK `query_trajectory` jq tool bound to a single trajectory JSON.

    Each review uses its own instance because `_page_origins` stores state for
    pagination continuations across calls. ADK executes tool calls from a single
    model turn concurrently, so `_page_origins_lock` guards access to
    `_page_origins`.
    """

    def __init__(self, trajectory_json: dict[str, Any]) -> None:
        """Initialize the tool for `trajectory_json`.

        Args:
            trajectory_json: Trajectory JSON built by
                `trajectory_json.build_trajectory_json`.
        """
        self._declaration = build_query_declaration()
        super().__init__(
            name=QUERY_TOOL, description=self._declaration.description or ""
        )
        self._trajectory_text = _format_worker_input(trajectory_json)
        self._page_origins: dict[str, PageOrigin] = {}
        self._page_origins_lock = threading.Lock()

    def _get_declaration(self) -> types.FunctionDeclaration:
        """Return the tool's function declaration.

        Returns:
            `query_trajectory` function declaration.
        """
        return self._declaration

    def query_trajectory(self, expr: str) -> ToolOutput:
        """Run a jq expression against the trajectory JSON.

        Args:
            expr: jq expression to evaluate.

        Returns:
            Query output capped at `QUERY_OUTPUT_CHARS` (with a pagination
            continuation note when truncated), or an error `ToolOutput` on jq
            compile/runtime errors or timeouts so the model can correct `expr`.
        """
        reply = run_jq(self._trajectory_text, expr)
        if "error" in reply:
            partial = reply.get("text") or ""
            return ToolOutput(
                reply["error"]
                + (f"\noutput before the error:\n{partial}" if partial else ""),
                is_error=True,
            )
        text = reply.get("text") or (
            "(one empty string)"
            if reply.get("results_total")
            else "(no results)"
        )
        if reply.get("truncated"):
            with self._page_origins_lock:
                origin = self._page_origins.get(expr.strip())
            note = render_truncation_note(expr, reply, origin)
            with self._page_origins_lock:
                self._page_origins.update(note.continuations)
            text += "\n" + note.text
        return ToolOutput(text)

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, str]:
        """Execute a jq query asynchronously on a worker thread.

        Offloads `query_trajectory` to a thread so parallel tool calls within a
        single model reply can overlap.

        Args:
            args: Tool call arguments containing ``expr``.
            tool_context: ADK tool execution context (unused).

        Returns:
            ``{"output": text}`` on success or ``{"error": text}`` on failure,
            matching the standard ADK function response keys.
        """
        del tool_context
        output = await asyncio.to_thread(
            self.query_trajectory, str(args.get("expr") or "")
        )
        return {"error" if output.is_error else "output": output.text}


def _format_worker_input(trajectory_json: dict[str, Any]) -> str:
    """Serialize `trajectory_json` for a worker process's stdin.

    Replaces lone UTF-16 surrogates (which UTF-8 cannot encode and jq cannot
    parse as escapes) with U+FFFD so character offsets stay aligned.

    Args:
        trajectory_json: Trajectory JSON built by
            `trajectory_json.build_trajectory_json`.

    Returns:
        Serialized trajectory JSON.
    """
    return _SURROGATE.sub(
        "\ufffd", json.dumps(trajectory_json, ensure_ascii=False, default=str)
    )


def run_jq(trajectory_text: str, expr: str) -> dict[str, Any]:
    """Run a jq expression in `_jq_worker.py` with a wall-clock timeout.

    Acquires one of `WORKER_CONCURRENCY` slots before starting the subprocess
    and its timeout timer. Output is capped at `QUERY_OUTPUT_CHARS`.

    Args:
        trajectory_text: Serialized trajectory JSON.
        expr: jq expression to evaluate.

    Returns:
        Parsed worker reply dictionary, or ``{"error": ...}`` on timeout,
        resource limit, or worker failure.
    """
    request = {
        "expr": expr,
        "max_chars": QUERY_OUTPUT_CHARS,
        "count_chars": QUERY_COUNT_CHARS,
        "count_seconds": QUERY_COUNT_SECONDS,
    }
    try:
        with _WORKER_SLOTS:
            completed = subprocess.run(  # noqa: S603 - fixed argv; the input travels on stdin
                # 1. Only `_WORKER_ENV_VARIABLES` reach the worker, and the
                #    worker clears them before jq runs, so jq's `$ENV` and
                #    `env` cannot expose parent credentials.
                # 2. `-I` omits the script's directory from `sys.path` so the
                #    worker imports the third-party `jq` bindings rather than
                #    this module.
                # 3. `-X utf8` matches the UTF-8 encoding used on stdin and
                #    stdout.
                [sys.executable, "-I", "-X", "utf8", str(_JQ_WORKER)],
                env={
                    name: os.environ[name]
                    for name in _WORKER_ENV_VARIABLES
                    if name in os.environ
                },
                input=json.dumps(request) + "\n" + trajectory_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=QUERY_TIMEOUT_SECONDS,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return {
            "error": (
                f"jq error: the expression ran longer than "
                f"{QUERY_TIMEOUT_SECONDS} s and was stopped; simplify it"
            )
        }
    except (OSError, UnicodeError) as exc:
        return {"error": f"jq worker failed: {exc}"}
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        # Past its address-space limit jq aborts with "cannot allocate memory"
        # and Python raises `MemoryError`; past its CPU limit the kernel kills
        # the process with a signal.
        if (
            completed.returncode < 0
            or "cannot allocate memory" in stderr
            or "MemoryError" in stderr
        ):
            return {
                "error": (
                    "jq error: the expression needed more than "
                    f"{_jq_worker.MEMORY_LIMIT_BYTES >> 20} MiB of memory or "
                    f"{_jq_worker.CPU_LIMIT_SECONDS} s of CPU time and was "
                    "stopped; simplify it"
                )
            }
        detail = stderr.strip().splitlines()[-1:] or [
            f"exit code {completed.returncode}"
        ]
        return {"error": f"jq worker failed: {detail[0]}"}
    try:
        reply = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"error": "jq worker returned no result"}
    return reply if isinstance(reply, dict) else {"error": "jq worker failed"}


def render_truncation_note(
    expr: str, reply: dict[str, Any], origin: PageOrigin | None = None
) -> TruncationNote:
    """Render a pagination note for a truncated jq query result.

    Args:
        expr: jq expression evaluated by the worker.
        reply: Worker reply dictionary.
        origin: `PageOrigin` if `expr` was a continuation from an earlier note,
            or None for a fresh expression.

    Returns:
        `TruncationNote` reporting the returned range in absolute positions and
        mapping each continuation expression to its `PageOrigin`.
    """
    if reply.get("single_string"):
        return _render_string_note(expr, reply, origin)
    if origin is None or origin.kind != _RESULTS_PAGE:
        origin = PageOrigin(_RESULTS_PAGE, expr.strip(), 0)
    if reply.get("first_cut"):
        return _render_first_result_note(reply, origin)
    return _render_results_note(reply, origin)


def _render_string_note(
    expr: str, reply: dict[str, Any], origin: PageOrigin | None
) -> TruncationNote:
    """Render a truncation note for a single string result.

    Plain paths are continued by slicing the path directly (from which later
    calls parse the start and end offsets); other expressions are wrapped in
    parentheses and tracked in `TruncationNote.continuations`.

    Args:
        expr: jq expression evaluated by the worker.
        reply: Worker reply dictionary.
        origin: `PageOrigin` of `expr`, or None.

    Returns:
        `TruncationNote` for the truncated string. Plain-path continuations omit
        `continuations` entries because their slice syntax encodes the offsets.
    """
    returned = len(reply.get("text") or "")
    total = int(reply.get("total_chars") or 0)
    match = _match_sliceable_path(expr) if origin is None else None
    end = ""
    if origin is not None and origin.kind == _STRING_PAGE:
        base, start, whole = origin.base, origin.offset, False
    elif origin is not None:
        # The last result of a results page: continue inside that one result.
        base = f"[{_close_comment(origin.base)}][{origin.offset}]"
        start, whole = 0, True
    elif match:
        base, end = match["base"], match["end"] or ""
        start = int(match["start"] or 0)
        whole = match["start"] is None
    else:
        base, start, whole = expr.strip(), 0, True
    resume = start + returned
    continuations: dict[str, PageOrigin] = {}
    if match:
        continuation = f"{base}[{resume}:{end}]"
    else:
        continuation = f"({_close_comment(base)})[{resume}:]"
        continuations = {continuation: PageOrigin(_STRING_PAGE, base, resume)}
    span = (
        f"returned chars 0-{returned} of {total}"
        if whole
        else f"returned chars {start}-{resume} of chars {start}-{start + total}"
    )
    return TruncationNote(
        f"[truncated: {span}; continue with `{continuation}`]", continuations
    )


def _render_first_result_note(
    reply: dict[str, Any], origin: PageOrigin
) -> TruncationNote:
    """Render a truncation note when the first result alone exceeds the output cap.

    Args:
        reply: Worker reply containing a prefix of the first result.
        origin: `PageOrigin` for the result stream.

    Returns:
        `TruncationNote` with a continuation to read the rest of the first
        result, plus a second continuation for remaining results when present.
    """
    base = _close_comment(origin.base)
    offset = origin.offset
    returned = len(reply.get("text") or "")
    size = int(reply.get("first_chars") or 0)
    count = int(reply.get("results_total") or 0)
    capped = bool(reply.get("total_capped"))
    if offset == 0 and count == 1 and not capped:
        value = f"{base} | tojson"
        rest = f"({value})[{returned}:]"
        return TruncationNote(
            f"[truncated: returned chars 0-{returned} of the value's {size} "
            f"JSON chars; continue with `{rest}`, or list its keys with "
            f"`{base} | keys`]",
            {rest: PageOrigin(_STRING_PAGE, value, returned)},
        )
    item = f"[{base}][{offset}]"
    is_string = bool(reply.get("first_is_string"))
    value = item if is_string else f"{item} | tojson"
    rest = f"({value})[{returned}:]"
    continuations = {rest: PageOrigin(_STRING_PAGE, value, returned)}
    unit = "chars" if is_string else "JSON chars"
    text = (
        f"[truncated: result {offset} alone is {size} {unit}, returned its "
        f"chars 0-{returned}; read the rest with `{rest}`"
    )
    if capped or count > 1:
        following = f"[{base}][{offset + 1}:][]"
        continuations[following] = PageOrigin(
            _RESULTS_PAGE, origin.base, offset + 1
        )
        text += f", then continue with `{following}`"
    return TruncationNote(text + "]", continuations)


def _render_results_note(
    reply: dict[str, Any], origin: PageOrigin
) -> TruncationNote:
    """Render a truncation note when a multi-result stream is cut between results.

    Args:
        reply: Worker reply dictionary.
        origin: `PageOrigin` for the result stream.

    Returns:
        `TruncationNote` with absolute result indices and a continuation
        expression starting at the next unread result.
    """
    first = origin.offset
    returned = int(reply.get("results_returned") or 0)
    count = int(reply.get("results_total") or 0)
    total = int(reply.get("total_chars") or 0)
    capped = bool(reply.get("total_capped"))
    bound = "more than " if capped else ""
    # The size covers only the results from the page's first one on.
    scope = f" from result {first}" if first else ""
    following = f"[{_close_comment(origin.base)}][{first + returned}:][]"
    return TruncationNote(
        f"[truncated: returned results {first}-{first + returned - 1} of "
        f"{bound}{first + count} ({bound}{total} chars{scope}); continue with "
        f"`{following}`]",
        {following: PageOrigin(_RESULTS_PAGE, origin.base, first + returned)},
    )


def _match_sliceable_path(expr: str) -> re.Match[str] | None:
    """Match a plain path that may end in a string slice.

    Args:
        expr: The jq expression the model sent.

    Returns:
        The `_SLICEABLE_PATH` match, or None when `expr` is not such a path.
    """
    match = _SLICEABLE_PATH.fullmatch(expr)
    # `[:]` matches the pattern but is not a jq slice.
    if match and match["start"] == "" and match["end"] == "":
        return None
    return match


def _close_comment(expr: str) -> str:
    """Append a blank line to `expr` if it contains a `#` comment.

    Continuation expressions wrap `expr` inside ``(...)`` or ``[...]``. Adding
    two newlines prevents a trailing comment (including one ending with a
    backslash line continuation) from commenting out the closing delimiter.

    Args:
        expr: jq expression.

    Returns:
        `expr` with two trailing newlines if `#` is present, otherwise `expr`
        unchanged.
    """
    return expr + "\n\n" if "#" in expr else expr
