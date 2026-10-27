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

"""Which attached agent an observed engine belongs to, as the engine and
`publish-source` both decide it."""

from __future__ import annotations

import pytest
from ambient_quality_shared import engine_agent

_STAGING = "acme-staging__bot"
_PROD = "acme-prod__bot"


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("42", "42"),
        ("projects/p/locations/l/reasoningEngines/42", "42"),
        ("projects/p/locations/l/reasoningEngines/42/", "42"),
        ("", ""),
    ],
)
def test_the_engine_id_is_the_trailing_segment(
    engine: str, expected: str
) -> None:
    assert engine_agent.extract_engine_id(engine) == expected


def test_the_attachment_recording_the_engine_wins() -> None:
    """Staging and prod share a name; the engine tells them apart."""
    recorded = {
        _PROD: "1",
        _STAGING: "projects/acme-staging/locations/l/reasoningEngines/2",
    }

    assert (
        engine_agent.resolve_engine_agent_key("2", recorded, _PROD) == _STAGING
    )


def test_of_several_attachments_recording_the_engine_the_first_wins_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorded = {_PROD: "1", _STAGING: "1"}

    with caplog.at_level("WARNING", logger=engine_agent.__name__):
        assert engine_agent.resolve_engine_agent_key("1", recorded, "") == _PROD

    assert any(_STAGING in r.message for r in caplog.records)


def test_aquas_own_engine_is_no_agents() -> None:
    """Even the default agent's, which may record no engine."""
    assert (
        engine_agent.resolve_engine_agent_key(
            "99",
            {_PROD: ""},
            _PROD,
            aqua_engine_id="projects/p/locations/l/reasoningEngines/99",
        )
        == ""
    )


def test_the_default_agent_recording_no_engine_may_be_the_engines() -> None:
    assert (
        engine_agent.resolve_engine_agent_key("3", {_PROD: ""}, _PROD) == _PROD
    )


def test_the_environments_unattached_agent_may_be_the_engines() -> None:
    """It has nothing stored, so no engine it could record."""
    assert (
        engine_agent.resolve_engine_agent_key("3", {_PROD: "1"}, "bot") == "bot"
    )


def test_the_default_agent_recording_another_engine_is_not_chosen() -> None:
    """Its root-cause analysis would cite this engine's code as its own."""
    assert engine_agent.resolve_engine_agent_key("3", {_PROD: "1"}, _PROD) == ""


def test_with_no_default_agent_no_agent_is_chosen() -> None:
    assert engine_agent.resolve_engine_agent_key("3", {}, "") == ""
