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

"""The Studio's "Check images" and "Check videos" switches: off by default,
one setting each, passed to the tools, which keep them on by default."""

from __future__ import annotations

import inspect
import json

import pytest

from genmedia4commerce import tools
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import VariationEvent
from genmedia4commerce.core.types import Verdict, unchecked
from genmedia4commerce.studio import agent_settings, job_call, job_events, turn
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import prompt as sp
from genmedia4commerce.studio import state as ss

OFF = {"check_images": False, "check_videos": False}


def test_the_checks_are_off_and_kept_once_for_every_page(
    storage, monkeypatch
) -> None:
    del storage  # the state lives in the fake bucket
    monkeypatch.setattr(sh, "note_later", lambda studio_id, text: True)
    assert rules.tryon_options({}, rules.CHECKS) == OFF
    st = ss.apply_action(
        "s1", {"settings": {"check_videos": True}, "page": "spin"}
    )
    assert st["state"]["settings"] == {"tryon": {"check_videos": True}}
    with pytest.raises(InputError):  # a switch takes true or false only
        ss.apply_action("s1", {"settings": {"check_images": "yes"}})
    assert rules.tryon_options({"check_images": 1}, rules.CHECKS) == OFF


def test_each_tool_gets_its_checks_off_unless_turned_on(monkeypatch) -> None:
    monkeypatch.setattr(sm, "resolve", lambda ref: ref)

    def checks(cap: str, **settings: bool) -> dict:
        spec = {"capability": cap, "studio_id": "s1", "settings": settings}
        kw = job_call.tool_call(spec)[1]
        return {k: kw[k] for k in OFF if k in kw}

    assert checks("tryon") == {"check_images": False}
    assert checks("video_tryon") == OFF
    assert checks("spin") == {"check_videos": False}
    on = {"check_images": True, "check_videos": True}
    assert checks("tryon", **on) == {"check_images": True}
    assert checks("video_tryon", **on) == on
    assert checks("spin", **on) == {"check_videos": True}


@pytest.mark.parametrize(
    ("tool", "keys"),
    [
        (tools.try_on, ["check_images"]),
        (tools.video_try_on, ["check_images", "check_videos"]),
        (tools.spin_360, ["check_videos"]),
    ],
)
def test_the_tools_keep_their_checks_on_by_default(tool, keys) -> None:
    params = inspect.signature(tool).parameters
    assert [params[k].default for k in keys] == [True] * len(keys)


def test_the_agent_turns_the_checks_on_and_off() -> None:
    sc = turn.Scope(studio_id="s1", st={"capability": "spin"}, results={})
    token = turn.start(sc)
    try:
        out = agent_settings.update_settings(check_videos="on")
        assert out == {"settings": {"check_videos": True}}
        agent_settings.update_settings(check_images="off", check_videos="?")
        assert sc.st["settings"] == {
            "tryon": {"check_videos": True, "check_images": False}
        }
    finally:
        turn.end(token)
    text = sp.summary(sc.st)
    on = {"check_images": False, "check_videos": True}
    assert f"checks: {json.dumps(on)}" in text


def test_an_unchecked_result_has_no_score() -> None:
    def result(verdict: Verdict) -> dict:
        event = VariationEvent(0, "ready", None, verdict, 0.0)
        event.media_uri = "gs://bucket/runs/out.png"
        return job_events.result(event.to_dict(), False) or {}

    shown = result(unchecked())
    assert (shown["score"], shown["note"]) == (None, "Not checked")
    assert result(Verdict(True, description="ok"))["score"] == 0.0
