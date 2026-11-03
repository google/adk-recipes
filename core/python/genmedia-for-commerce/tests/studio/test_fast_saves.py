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

"""A user action is answered before its history note is written, and
preview URLs are made in parallel."""

import threading
import time
from typing import Any

import pytest

from genmedia4commerce.core.events import VariationEvent
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import job_events, patches, sessions, store
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio.events import CAPABILITY_NAMES

_RUN = "runs/2026-01-02/studio-s1/0123456789ab"


def media(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


def output(name: str) -> str:
    return media(f"{_RUN}/outputs/{name}")


@pytest.fixture(autouse=True)
def _fast(storage, monkeypatch: pytest.MonkeyPatch) -> None:
    del storage  # the state and the notes live in the fake bucket
    monkeypatch.setattr(store, "_BACKOFF_S", 0)


def _texts(events: list[Any]) -> list[str]:
    return [e.content.parts[0].text for e in events]


def test_the_answer_does_not_wait_for_the_note(monkeypatch) -> None:
    release = threading.Event()
    written: list[Any] = []

    def add_event(studio_id: str, event: Any) -> bool:
        assert release.wait(5)
        written.append(event)
        return True

    monkeypatch.setattr(sessions, "add_event", add_event)
    try:
        before = time.time()
        ss.apply_action("s1", {"capability": "spin"})
        ss.apply_action("s1", {"capability": "video_tryon"})
        after = time.time()
        assert written == []  # both answered, neither note written yet
    finally:
        release.set()
    assert sh.flush(5)
    events = sorted(written, key=lambda e: e.timestamp)
    assert all(before <= e.timestamp <= after for e in events)
    assert _texts(events) == [
        f"{sh.STUDIO_PREFIX}User switched to the {CAPABILITY_NAMES[c]} page."
        for c in ("spin", "video_tryon")
    ]


@pytest.mark.asyncio
async def test_notes_written_later_keep_their_order() -> None:
    for cap in ("spin", "video_tryon", "tryon"):
        ss.apply_action("s1", {"capability": cap})
    assert sh.flush(5)
    session = await sessions.StudioSessionService().get_session(
        app_name=sessions.APP_NAME, user_id=sessions.PANEL_USER, session_id="s1"
    )
    assert session is not None
    assert _texts(session.events) == [
        f"{sh.STUDIO_PREFIX}User switched to the {CAPABILITY_NAMES[c]} page."
        for c in ("spin", "video_tryon", "tryon")
    ]


def test_catalogue_copies_and_descriptions_are_read_together(
    monkeypatch, catalog_file
) -> None:
    del catalog_file
    both = threading.Barrier(2, timeout=5)  # broken if one waits alone
    copy = media("runs/studio_catalog/m_emma/0.webp")

    def copies(refs: list[str]) -> dict:
        both.wait()
        return {r: (copy, "a model") for r in refs if r.startswith("catalog:")}

    def descriptions(refs: list[str]) -> dict:
        both.wait()
        return {}

    monkeypatch.setattr(sm, "catalog_copies", copies)
    monkeypatch.setattr(sm, "upload_descriptions", descriptions)
    monkeypatch.setattr(sh, "note_later", lambda sid, text: None)
    item = {"label": "Emma", "ref": "catalog:m_emma"}
    out = ss.apply_action("s1", {"set": {"field": "person", "item": item}})
    assert out["refs"] == {"catalog:m_emma": copy}
    assert out["state"]["person"]["ref"] == copy


def _signer(monkeypatch, parties: int, bad: str) -> list[str]:
    """``sm.preview_url`` that answers only once ``parties`` calls are in
    at once (so the test fails, not hangs, when they come one by one) and
    fails for ``bad``; returns the refs it was called with."""
    together = threading.Barrier(parties, timeout=5)
    seen: list[str] = []

    def preview_url(uri: str, video: bool = False) -> str:
        seen.append(uri)
        together.wait()
        if uri == bad:
            raise RuntimeError("cannot sign")
        return f"https://signed/{uri}?video={video}"

    monkeypatch.setattr(sm, "preview_url", preview_url)
    return seen


def test_saved_state_signs_every_list_at_once(monkeypatch) -> None:
    a, b, c, d = (output(f"{n}.png") for n in "abcd")
    state = {
        "capability": "tryon",
        "results": {"tryon": [{"uri": a}, {"uri": b}], "spin": [{"uri": c}]},
        "images": {"video_tryon": [{"uri": d}, {"no": "uri"}]},
    }
    store.write(store.state_path("s1"), {"state": state, "updated_at": 1})
    _signer(monkeypatch, 4, bad=b)
    st = ss.saved_state("s1")["state"]
    tryon, spin = st["results"]["tryon"], st["results"]["spin"]
    assert [r["url"] for r in tryon] == [f"https://signed/{a}?video=False", ""]
    assert spin[0]["url"] == f"https://signed/{c}?video=True"
    assert st["images"]["video_tryon"] == [
        {"uri": d, "url": f"https://signed/{d}?video=False"},
        {"no": "uri"},
    ]


def test_previews_are_made_in_parallel(monkeypatch) -> None:
    refs = [output("a.png"), output("b.mp4"), output("c.png")]
    _signer(monkeypatch, 3, bad=refs[2])
    out = sm.previews([*refs, "gs://other/x.png"])
    assert list(out) == refs[:2]  # the failed and the foreign are left out
    assert out[refs[1]] == f"https://signed/{refs[1]}?video=True"


def _signed(start: float, expires: int) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start))
    return (
        "https://storage.googleapis.com/b/o.mp4?X-Goog-Algorithm=GOOG4-RSA"
        f"&X-Goog-Date={stamp}&X-Goog-Expires={expires}&X-Goog-Signature=ab"
    )


def test_a_url_valid_for_half_an_hour_more_is_kept(monkeypatch) -> None:
    """A finished job's status and a restore keep a fresh URL, so a
    playing video does not reload; an old one is signed again."""
    signed: list[str] = []

    def preview_url(uri: str, *, video: bool = False) -> str:
        signed.append(uri)
        return "new"

    monkeypatch.setattr(sm, "preview_url", preview_url)
    now = time.time()
    fresh = {"uri": "gs://b/fresh.mp4", "url": _signed(now - 60, 3600)}
    old = {"uri": "gs://b/old.mp4", "url": _signed(now - 3000, 3600)}
    bare = {"uri": "gs://b/bare.mp4", "url": "data:image/png;base64,AA"}

    sm.refresh_urls([([fresh, old, bare], True)])

    assert fresh["url"].startswith("https://") and old["url"] == "new"
    assert sorted(signed) == ["gs://b/bare.mp4", "gs://b/old.mp4"]


def test_a_spins_mp4_gets_its_own_url(monkeypatch) -> None:
    """A spin result names its MP4 (``video_uri``, kept with the studio)
    and a URL for it, renewed like its preview's; other results have
    none."""
    signed: list[tuple[str, bool]] = []

    def preview_url(uri: str, *, video=False, inline=True) -> str:
        signed.append((uri, inline))
        return f"signed:{uri}"

    monkeypatch.setattr(sm, "preview_url", preview_url)
    spin = VariationEvent(
        0, "ready", None, None, 1.0, media_uri="gs://b/0.webp"
    )
    spin.video_uri = "gs://b/0.mp4"

    shown = job_events.result(spin.to_dict(), True) or {}

    assert shown["video_url"] == "signed:gs://b/0.mp4"
    kept = patches.clean(shown, patches.RESULT_KEYS)
    assert kept["video_uri"] == "gs://b/0.mp4" and "video_url" not in kept
    sm.refresh_urls([([kept], False)])
    assert kept["video_url"] == "signed:gs://b/0.mp4"
    assert signed.count(("gs://b/0.mp4", False)) == 2  # never inlined
    image = VariationEvent(
        0, "ready", None, None, 1.0, media_uri="gs://b/0.png"
    )
    assert "video_url" not in (job_events.result(image.to_dict(), False) or {})


def test_an_mp4_has_no_url_without_signing(monkeypatch) -> None:
    """Without signed URLs (user credentials) a spin's MP4 gets no link:
    it is never inlined in the studio's responses like a preview."""

    def unsigned(uri: str) -> str:
        raise RuntimeError("no signing")

    monkeypatch.setattr(sm.gcs, "signed_url", unsigned)
    monkeypatch.setattr(sm, "allowed_ref", lambda uri: True)
    monkeypatch.setattr(sm.gcs, "resolve_image", lambda uri: b"mp4 bytes")

    assert sm.preview_url("gs://b/0.mp4", inline=False) == ""
    assert sm.preview_url("gs://b/0.mp4", video=True).startswith("data:")


def test_a_chat_turn_waits_for_the_notes_first(monkeypatch) -> None:
    import asyncio

    from genmedia4commerce.studio import agent, service

    order: list[str] = []
    monkeypatch.setattr(sh, "flush", lambda timeout=10.0: order.append("flush"))

    async def run_agent_turn(message: str, studio_id: str):
        order.append("turn")
        raise RuntimeError("stop here")

    monkeypatch.setattr(agent, "run_agent_turn", run_agent_turn)
    with pytest.raises(RuntimeError):
        asyncio.run(service.agent_turn("hello", "aaaabbbbcccc"))
    assert order == ["flush", "turn"]
