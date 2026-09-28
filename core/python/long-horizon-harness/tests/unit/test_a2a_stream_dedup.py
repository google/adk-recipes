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

"""The A2A event converter must put streamed text on the wire exactly once.

ADK's v2 converter emits incremental delta artifact-updates AND a final
full-text aggregate. Conformant clients honor append/last_chunk; naive ones
(Gemini Enterprise) concatenate every part and double the reply. We suppress
the redundant aggregate when the run was streamed.

Parts/events are built + read via ADK's ``_compat`` bridge (proto on a2a 1.x).
"""

from __future__ import annotations

from google.adk.a2a import _compat as a2a_compat
from google.adk.events.event import Event
from google.genai import types

from app.a2a.executor import (
    _StreamDedupConverter,
    _strip_fake_artifact_links,
    _surface_tool_calls,
)


def _artifact_tool_event(adk_type: str, *, call_id: str, name: str):
    """A raw new-impl artifact-update carrying one tool DataPart, pre-surfacing."""
    payload = (
        {"args": {"x": 1}}
        if adk_type == "function_call"
        else {"response": {"ok": True}}
    )
    part = a2a_compat.make_data_part(
        data={"id": call_id, "name": name, **payload},
        metadata={"adk_type": adk_type},
    )
    return a2a_compat.TaskArtifactUpdateEvent(
        task_id="t1",
        context_id="ctx1",
        artifact=a2a_compat.make_artifact(artifact_id="a1", parts=[part]),
    )


def _status_messages(a2a_events):
    out = []
    for ev in a2a_events:
        if getattr(ev, "artifact", None) is not None:
            continue
        msg = getattr(getattr(ev, "status", None), "message", None)
        if msg is not None and (msg.parts or []):
            out.append(msg)
    return out


def _msg_datas(a2a_events):
    """Data dicts carried by status-message DataParts."""
    return [
        a2a_compat.data_part_dict(p)
        for m in _status_messages(a2a_events)
        for p in m.parts
        if a2a_compat.is_data_part(p)
    ]


def _msg_texts(a2a_events):
    return [
        a2a_compat.part_text(p)
        for m in _status_messages(a2a_events)
        for p in m.parts
        if a2a_compat.is_text_part(p)
    ]


def _text_event(text: str, *, partial: bool) -> Event:
    return Event(
        author="root_agent",
        partial=partial,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


def _text_and_call_event(text: str) -> Event:
    # ADK closes a streamed run with the full text bundled alongside the turn's
    # function call in a single non-partial event.
    return Event(
        author="root_agent",
        partial=False,
        content=types.Content(
            role="model",
            parts=[
                types.Part(text=text),
                types.Part(
                    function_call=types.FunctionCall(
                        name="load_skill", args={"name": "policy"}
                    )
                ),
            ],
        ),
    )


def _artifact_parts(ev):
    art = getattr(ev, "artifact", None)
    return (getattr(art, "parts", None) or []) if art is not None else []


def _has_function_call(a2a_events) -> bool:
    for ev in a2a_events:
        for p in _artifact_parts(ev):
            if a2a_compat.is_data_part(p):
                return True
        for m in _status_messages([ev]):
            if any(a2a_compat.is_data_part(p) for p in m.parts):
                return True
    return False


def _text_of(a2a_events) -> list[str]:
    return [
        a2a_compat.part_text(p)
        for ev in a2a_events
        for p in _artifact_parts(ev)
        if a2a_compat.is_text_part(p)
    ]


def test_streamed_final_aggregate_is_suppressed():
    conv = _StreamDedupConverter()
    artifacts: dict[str, str] = {}
    emitted: list[str] = []

    for delta in ["Hey", "!", " there"]:
        emitted += _text_of(
            conv(_text_event(delta, partial=True), artifacts, "t1", "c1")
        )
    # Final non-partial event carries the full aggregate "Hey! there".
    final = conv(
        _text_event("Hey! there", partial=False), artifacts, "t1", "c1"
    )

    # Deltas are on the wire; the redundant aggregate is dropped.
    assert "".join(emitted) == "Hey! there"
    assert _text_of(final) == []


def test_nonstreamed_single_event_is_kept():
    conv = _StreamDedupConverter()
    artifacts: dict[str, str] = {}

    # No partials preceded it — the single final event IS the only copy.
    only = conv(
        _text_event("All at once.", partial=False), artifacts, "t1", "c1"
    )
    assert _text_of(only) == ["All at once."]


def test_streamed_final_drops_text_but_keeps_function_call():
    conv = _StreamDedupConverter()
    artifacts: dict[str, str] = {}
    emitted: list[str] = []

    for delta in ["I'll ", "load the skill."]:
        emitted += _text_of(
            conv(_text_event(delta, partial=True), artifacts, "t1", "c1")
        )
    final = conv(
        _text_and_call_event("I'll load the skill."), artifacts, "t1", "c1"
    )

    # Streamed text appears once; the redundant aggregate text is gone but the
    # turn's function call survives — relocated from the artifact to a status
    # message (see test_function_call_surfaces_as_status_message_not_artifact).
    assert "".join(emitted) == "I'll load the skill."
    assert _text_of(final) == []
    assert _has_function_call(final)


def _function_call_in_artifact(a2a_events) -> bool:
    return any(
        a2a_compat.is_data_part(p)
        for ev in a2a_events
        for p in _artifact_parts(ev)
    )


def test_function_call_surfaces_as_status_message_not_artifact():
    # New-impl converter buries tool calls in artifacts; they must leave as a
    # working status-update message instead.
    conv = _StreamDedupConverter()
    artifacts: dict[str, str] = {}

    out = conv(_text_and_call_event("Running it."), artifacts, "t1", "c1")

    assert not _function_call_in_artifact(out)
    assert any(
        any(a2a_compat.is_data_part(p) for p in m.parts)
        for m in _status_messages(out)
    ), "function call must ride a status-update message"


def test_tool_call_surfaces_as_datapart_without_label():
    # The call leaves the converter as a structured DataPart on a working status
    # message — no fabricated text label. The web renders the chip from the part
    # (and dedups the partial/final double-emit by callId in chat-segments.ts).
    out = _surface_tool_calls(
        [_artifact_tool_event("function_call", call_id="c1", name="terminal")],
        "t1",
        "ctx1",
    )
    assert _msg_datas(out), (
        "function_call must ride a status message as a DataPart"
    )
    assert not _msg_texts(out), "no fabricated tool label"


def test_surfaced_tool_name_is_ge_prefixed():
    # GE's timeline drops any tool whose name isn't <agent>__<action>, so a flat
    # name gets the horizon__ prefix on the way out (the web strips it back).
    for adk_type in ("function_call", "function_response"):
        out = _surface_tool_calls(
            [_artifact_tool_event(adk_type, call_id="c1", name="terminal")],
            "t1",
            "ctx1",
        )
        names = [d.get("name") for d in _msg_datas(out)]
        assert names == ["horizon__terminal"], adk_type


def _text_artifact_event(text: str):
    return a2a_compat.TaskArtifactUpdateEvent(
        task_id="t1",
        context_id="ctx1",
        artifact=a2a_compat.make_artifact(
            artifact_id="a1", parts=[a2a_compat.make_text_part(text)]
        ),
    )


def _artifact_texts(events):
    return [
        a2a_compat.part_text(p)
        for ev in events
        for p in _artifact_parts(ev)
        if a2a_compat.is_text_part(p)
    ]


def test_fake_attachment_link_stripped_to_text():
    # The model invents [name](attachment://name) because the real URL is
    # redacted from its view; no client renders it, so drop the dead href.
    out = _strip_fake_artifact_links(
        [
            _text_artifact_event(
                "Here's your plot: [plot.png](attachment://plot.png)"
            )
        ]
    )
    assert _artifact_texts(out) == ["Here's your plot: plot.png"]


def test_fake_sandbox_link_stripped_to_text():
    out = _strip_fake_artifact_links(
        [_text_artifact_event("[report.html](sandbox:/report.html)")]
    )
    assert _artifact_texts(out) == ["report.html"]


def test_real_https_link_preserved():
    # The 📎 surfaced link (a real signed GCS URL) must survive untouched.
    url = "https://storage.googleapis.com/lha-artifacts/x?sig=abc"
    out = _strip_fake_artifact_links(
        [_text_artifact_event(f"[plot.png]({url})")]
    )
    assert _artifact_texts(out) == [f"[plot.png]({url})"]


def test_framework_tool_names_stay_flat():
    # clarify / adk_request_confirmation are interaction prompts, not timeline
    # chips — the web matches them by exact name, so they keep their flat names.
    for name in ("clarify", "adk_request_confirmation"):
        out = _surface_tool_calls(
            [_artifact_tool_event("function_call", call_id="c1", name=name)],
            "t1",
            "ctx1",
        )
        assert [d.get("name") for d in _msg_datas(out)] == [name], name


def test_orphaned_append_demoted_to_create():
    # a2a 1.x rejects append=True for an artifact never created. When tool
    # surfacing drops an artifact's create event, a later streamed-text append
    # for that id is orphaned — it must be demoted to a create (append=False).
    conv = _StreamDedupConverter()
    ev = a2a_compat.TaskArtifactUpdateEvent(
        task_id="t1",
        context_id="c1",
        artifact=a2a_compat.make_artifact(
            artifact_id="orphan-1", parts=[a2a_compat.make_text_part("hi")]
        ),
    )
    ev.append = True
    out = conv._enforce_artifact_lifecycle([ev], "t1")
    assert out[0].append is False


def test_append_after_seen_create_is_preserved():
    conv = _StreamDedupConverter()
    create = a2a_compat.TaskArtifactUpdateEvent(
        task_id="t1",
        context_id="c1",
        artifact=a2a_compat.make_artifact(
            artifact_id="art-1", parts=[a2a_compat.make_text_part("a")]
        ),
    )
    conv._enforce_artifact_lifecycle(
        [create], "t1"
    )  # create seen (append=False)
    append = a2a_compat.TaskArtifactUpdateEvent(
        task_id="t1",
        context_id="c1",
        artifact=a2a_compat.make_artifact(
            artifact_id="art-1", parts=[a2a_compat.make_text_part("b")]
        ),
    )
    append.append = True
    out = conv._enforce_artifact_lifecycle([append], "t1")
    assert out[0].append is True  # valid append preserved
