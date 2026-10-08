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

The node tests replace this seam, so without these the whole bucket path -- the
config field, the state copy, the degrade-on-failure rule -- could be deleted
and the suite would stay green.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.state import WorkflowState

from .conftest import make_config

_CONFIG = b"metrics_to_run: [ok]\ncustom_metrics:\n  - name: ok\n    custom_function_file: ok.py\n"
_METRIC = b"EXPECTED = 'the published version'\n\n\ndef evaluate(i):\n    return 1.0\n"


def _ctx(**state: Any) -> Any:
    context = mock.MagicMock()
    context.state = state
    return context


@pytest.fixture
def stub_gcs(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make the factory's GCS read serve fixed objects, or raise."""

    def _stub(outcome: dict[str, bytes] | BaseException) -> None:
        def fake(bucket: str, **kwargs: Any) -> dict:
            if isinstance(outcome, BaseException):
                raise outcome
            from ambient_quality_agent.tools.metrics.library import load_library

            return load_library(outcome)

        monkeypatch.setattr(_common, "load_library_from_gcs", fake)

    return _stub


def test_no_bucket_means_no_metrics() -> None:
    """There is no baseline pack: nothing published means nothing scored, not
    a fallback to metrics nobody chose."""
    library, warning = _common.metric_library_factory(_ctx())

    assert library == {}
    assert warning == ""


def test_the_published_library_is_loaded(stub_gcs: Any) -> None:
    stub_gcs({"eval_config.yaml": _CONFIG, "ok.py": _METRIC})

    library, warning = _common.metric_library_factory(
        _ctx(metrics_gcs_bucket="b")
    )

    assert set(library) == {"ok"}
    assert library["ok"].expected == "the published version"
    assert warning == ""


def test_an_unreadable_bucket_degrades_instead_of_raising(
    stub_gcs: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad upload must not fail the node: the window only advances on a
    completed run, so it would stop every later sweep too."""
    stub_gcs(RuntimeError("403"))

    library, warning = _common.metric_library_factory(
        _ctx(metrics_gcs_bucket="b")
    )

    assert library == {}
    assert "Could not read the metric library" in caplog.text
    # ...and it must reach the run record, or a degraded sweep is
    # indistinguishable from a healthy one that found nothing.
    assert "could not be read" in warning


def test_a_module_that_exits_the_process_degrades_too(stub_gcs: Any) -> None:
    """`SystemExit` is not an `Exception`; an `argparse` call at module scope in
    an uploaded metric would otherwise stall every future sweep."""
    stub_gcs(SystemExit(2))

    library, warning = _common.metric_library_factory(
        _ctx(metrics_gcs_bucket="b")
    )

    assert library == {}
    assert "could not be read" in warning


def test_the_bucket_reaches_the_factory_from_config() -> None:
    """The whole wiring: env-backed `Config` field -> workflow state -> factory."""
    state = WorkflowState.from_config(
        make_config(metrics_gcs_bucket="my-metrics")
    )

    assert state.metrics_gcs_bucket == "my-metrics"


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

    with mock.patch.object(_common, "load_library_from_gcs", lambda b: library):
        loaded, warning = _common._create_default_metric_library(
            _ctx(metrics_gcs_bucket="some-bucket")
        )

    assert loaded is library
    assert "1 metric(s) did not load" in warning
    assert "`broken`" in warning
    assert "SyntaxError" in warning
