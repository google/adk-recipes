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

"""Unit test: GE-path converter lifts artifact links into text status messages.

Parts/events are built via ADK's ``_compat`` bridge so the test tracks the same
version-agnostic surface the converter consumes (proto on a2a 1.x).
"""

from __future__ import annotations

from google.adk.a2a import _compat as a2a_compat

from app.a2a.executor import _GE_LABEL_KEY, _surface_artifact_links


def _fn_response_event(response: dict, name: str = "artifact"):
    part = a2a_compat.make_data_part(
        data={"id": "c1", "name": name, "response": response},
        metadata={"adk_type": "function_response"},
    )
    return a2a_compat.TaskArtifactUpdateEvent(
        task_id="t1",
        context_id="ctx1",
        artifact=a2a_compat.make_artifact(
            artifact_id="a1", name=name, parts=[part]
        ),
    )


def _status_texts(events) -> list[str]:
    out = []
    for ev in events:
        msg = getattr(getattr(ev, "status", None), "message", None)
        if msg is None:
            continue
        for p in msg.parts:
            if a2a_compat.is_text_part(p):
                out.append(a2a_compat.part_text(p))
    return out


def test_artifact_response_becomes_text_link():
    ev = _fn_response_event(
        {"success": True, "filename": "plot.html", "url": "https://signed/x"}
    )
    out = _surface_artifact_links([ev], "t1", "ctx1")
    texts = _status_texts(out)
    assert any("plot.html" in t and "https://signed/x" in t for t in texts)
    # Blank-line wrapped so GE's no-separator concatenation doesn't jam the link
    # against the surrounding narration.
    link = next(t for t in texts if "plot.html" in t)
    assert link.startswith("\n\n") and link.endswith("\n\n")
    # Tagged GE-only: the web renders the artifact inline from its FilePart and
    # skips this link so the file isn't shown twice.
    link_part = next(
        p
        for ev in out
        if getattr(getattr(ev, "status", None), "message", None)
        for p in ev.status.message.parts
        if a2a_compat.is_text_part(p) and "plot.html" in a2a_compat.part_text(p)
    )
    assert a2a_compat.part_metadata(link_part).get(_GE_LABEL_KEY) is True


def test_non_artifact_response_emits_no_link():
    ev = _fn_response_event({"ok": True}, name="terminal")
    out = _surface_artifact_links([ev], "t1", "ctx1")
    assert _status_texts(out) == []


def test_artifact_response_without_url_emits_no_link():
    ev = _fn_response_event({"success": True, "filename": "plot.html"})
    out = _surface_artifact_links([ev], "t1", "ctx1")
    assert _status_texts(out) == []
