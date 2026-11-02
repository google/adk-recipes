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

"""Shared UI labels, spoken state phrases, and next-step prompts for threads."""

from __future__ import annotations

import re

from app.app_utils.markdown import drop_links, markdown_lines, strip_emphasis
from app.threads.model import PermissionRequest, ThreadHandle, ThreadState

STATE_LABELS: dict[ThreadState, str] = {
    ThreadState.EXECUTING: "Running",
    ThreadState.AWAITING_PERMISSION: "Needs your permission",
    ThreadState.COMPLETED: "Completed",
    ThreadState.FAILED: "Failed",
    ThreadState.CANCELLED: "Cancelled",
}

STATE_PHRASES: dict[ThreadState, str] = {
    ThreadState.EXECUTING: "running",
    ThreadState.AWAITING_PERMISSION: "waiting for your permission",
    ThreadState.COMPLETED: "completed",
    ThreadState.FAILED: "failed",
    ThreadState.CANCELLED: "cancelled",
}

_RUNNING_NEXT = (
    "share any preliminary progress so far while noting what is still running "
    "(the next update arrives on its own, so don't check again). "
    "If they ask: message_thread to redirect, cancel_thread to stop."
)
_FINISHED_NEXT = (
    "offer a follow-up or retry. Only if the user asks for one: message_thread "
    "starts a new turn on the same thread."
)
# Gates name the question for the user first so the model does not call `approve` unasked.
NEXT_STEPS: dict[ThreadState, str] = {
    ThreadState.EXECUTING: _RUNNING_NEXT,
    ThreadState.AWAITING_PERMISSION: (
        "tell the user what permission is requested and ask yes or no first. "
        "Only after they confirm in the next turn: approve. "
        "If they say no: approve(approved=False, reason=...) or message_thread with their reason "
        "(the thread continues without it; don't cancel)."
    ),
    ThreadState.COMPLETED: _FINISHED_NEXT,
    ThreadState.FAILED: _FINISHED_NEXT,
    ThreadState.CANCELLED: _FINISHED_NEXT,
}


def state_phrase(handle: ThreadHandle) -> str:
    return STATE_PHRASES[handle.state]


def next_step(handle: ThreadHandle) -> str:
    return NEXT_STEPS[handle.state]


OUTPUT_LINES = {
    "pr": "Opened pull request “{title}”.",
    "branch": "Pushed “{title}”.",
    "doc": "Updated “{title}”.",
    "sheet": "Updated “{title}”.",
    "event": "Added “{title}” to your calendar.",
    "draft": "Saved the draft “{title}” in Gmail.",
}
_SAVED_LINE = "Saved “{title}” to Drive."
OUTPUT_NOUNS = {
    "pr": "a pull request",
    "branch": "a branch",
    "doc": "a Google Doc",
    "sheet": "a Google Sheet",
    "slides": "a Google Slides deck",
    "event": "an event",
    "draft": "a draft",
    "file": "a file",
}


def output_line(output: dict[str, str]) -> str:
    kind = output.get("kind", "file")
    title = output.get("title") or OUTPUT_NOUNS.get(kind, "a file")
    line = OUTPUT_LINES.get(kind, _SAVED_LINE).format(title=title)
    return (
        line if output.get("title") else line.replace("“", "").replace("”", "")
    )


def action_label(action_class: str) -> str:
    return action_class.replace("_", " ")


def permission_phrase(permission: PermissionRequest) -> str:
    return f"permission to run `{permission.command}`"


def thread_ref(handle: ThreadHandle) -> str:
    where = f" in {handle.project}"
    goal = short(handle.goal, 80)
    asked = (
        "" if goal.lower() == handle.title.lower() else f' (asked: "{goal}")'
    )
    return f'{handle.thread_id} "{handle.title}"{where}{asked}'


def thread_name(handle: ThreadHandle) -> str:
    return f"“{handle.title}”"


def home_path(handle: ThreadHandle, file: str) -> str:
    return f"{handle.project}/{file}"


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plain_summary(
    text: str | None, fallback: str = "", max_chars: int = 200
) -> str:
    """First paragraph of Markdown-ish text as one or two plain sentences."""
    if not text or not text.strip():
        return fallback
    txt = drop_links(text.strip())
    txt = re.sub(r"projects/\d+/locations/\S+", "the sandbox", txt)
    paragraphs = markdown_lines(txt)
    first = paragraphs[0] if paragraphs else txt
    first = strip_emphasis(first)
    first = re.sub(r"\s{2,}", " ", first).strip(" -•:")
    if not first:
        return fallback
    if len(first) <= max_chars:
        return first
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", first) if s]
    if len(sentences[0]) <= max_chars:
        summary = sentences[0]
        if (
            len(sentences) > 1
            and len(summary) + 1 + len(sentences[1]) <= max_chars
        ):
            summary = f"{summary} {sentences[1]}"
        return summary
    return first[: max_chars - 1].rstrip() + "…"


def first_line(text: str | None, max_chars: int = 200) -> str:
    return plain_summary(text, "", max_chars)
