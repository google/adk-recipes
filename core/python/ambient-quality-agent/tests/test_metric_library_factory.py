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

"""Tests for `_common.metric_library_factory`.

The node tests replace this seam, so without these the whole store path -- the
read through the metrics store, the degrade-on-failure rule -- could be deleted
and the suite would stay green.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.tools.metrics import publishing
from ambient_quality_agent.tools.objects import store as objects_store

_CONFIG = b"metrics_to_run: [ok]\ncustom_metrics:\n  - name: ok\n    custom_function_file: ok.py\n"
_METRIC = b"EXPECTED = 'the published version'\n\n\ndef evaluate(i):\n    return 1.0\n"


def _ctx(**state: Any) -> Any:
    context = mock.MagicMock()
    context.state = state
    return context


@pytest.fixture
def stub_library(monkeypatch: pytest.MonkeyPatch, metrics_store: Any) -> Any:
    """Publish fixed objects to the metrics store, or make reading it raise."""

    def _stub(outcome: dict[str, bytes] | BaseException) -> None:
        if isinstance(outcome, BaseException):

            def fail(store: Any, agent_key: str) -> Any:
                raise outcome

            monkeypatch.setattr(publishing, "load_published_library", fail)
            return
        for path, data in outcome.items():
            metrics_store.write_bytes(publishing.LIBRARY_PREFIX + path, data)

    return _stub


def test_no_metrics_store_means_no_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no baseline pack: nothing published means nothing scored, not
    a fallback to metrics nobody chose."""
    monkeypatch.setattr(objects_store, "metrics_store_factory", lambda: None)

    library, warning = _common.metric_library_factory(_ctx())

    assert library == {}
    assert warning == ""


def test_the_published_library_is_loaded(stub_library: Any) -> None:
    stub_library({"eval_config.yaml": _CONFIG, "ok.py": _METRIC})

    library, warning = _common.metric_library_factory(_ctx())

    assert set(library) == {"ok"}
    assert library["ok"].expected == "the published version"
    assert warning == ""


def test_an_unreadable_library_degrades_instead_of_raising(
    stub_library: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad upload must not fail the node: the window only advances on a
    completed run, so it would stop every later sweep too."""
    stub_library(RuntimeError("403"))

    library, warning = _common.metric_library_factory(_ctx())

    assert library == {}
    assert "Could not read the metric library" in caplog.text
    # ...and it must reach the run record, or a degraded sweep is
    # indistinguishable from a healthy one that found nothing.
    assert "could not be read" in warning


def test_a_module_that_exits_the_process_degrades_too(
    stub_library: Any,
) -> None:
    """`SystemExit` is not an `Exception`; an `argparse` call at module scope in
    an uploaded metric would otherwise stall every future sweep."""
    stub_library(SystemExit(2))

    library, warning = _common.metric_library_factory(_ctx())

    assert library == {}
    assert "could not be read" in warning


def test_the_bucket_is_not_part_of_an_agents_configuration() -> None:
    """Whatever the bucket holds is executed here on this process's credentials.

    Anyone who could repoint it would be running their own code on the engine's
    identity, so it stays on the AQuA side of the split, where changing it costs
    the access that changing the deployment costs -- not in the object that
    attaching an agent writes. The mode is agent-side: choosing between
    evaluations decides nothing about what code runs.
    """
    agent_fields = {f.name for f in dataclasses.fields(ObservedAgentConfig)}

    assert "metrics_gcs_bucket" not in agent_fields
    assert "quality_analysis_mode" in agent_fields


def test_a_metric_that_did_not_load_is_named_in_the_warning() -> None:
    """The metrics that did load go on scoring every session, so without this
    the run reads as a clean sweep of the whole library rather than of what
    survived."""
    from ambient_quality_agent.tools.metrics.library import MetricLibrary

    library = MetricLibrary()
    library.failures = (("broken", "SyntaxError: '(' was never closed"),)

    with mock.patch.object(
        publishing, "load_published_library", lambda store, agent_key: library
    ):
        loaded, warning = _common._create_default_metric_library(_ctx())

    assert loaded is library
    assert "1 metric(s) did not load" in warning
    assert "`broken`" in warning
    assert "SyntaxError" in warning


def test_a_run_scores_with_its_agents_library(metrics_store: Any) -> None:
    metrics_store.write_bytes(
        "acme-prod__support/current/metrics/eval_config.yaml", _CONFIG
    )
    metrics_store.write_bytes(
        "acme-prod__support/current/metrics/ok.py", _METRIC
    )

    library, warning = _common.metric_library_factory(
        _ctx(agent_key="acme-prod__support")
    )
    other, _ = _common.metric_library_factory(_ctx(agent_key="acme-staging__x"))

    assert set(library) == {"ok"}
    assert warning == ""
    assert other == {}
