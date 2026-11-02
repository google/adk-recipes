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

"""Tests for run-scoped logging."""

from __future__ import annotations

import logging

import pytest
from ambient_quality_agent.core import _logging


def test_run_id_scope_tags_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _logging.install_run_id_logging()
    # Memory rides on the tag; stub it so the assertion is deterministic.
    monkeypatch.setattr(
        _logging, "render_memory_summary", lambda: "cgroup 100 MB"
    )
    logger = logging.getLogger("test.aqa.logging")
    with caplog.at_level(logging.INFO):
        with _logging.tag_logs_with_run_id("abc123"):
            logger.info("hello")
    assert any(
        r.getMessage() == "[run_id=abc123 mem=cgroup 100 MB] hello"
        for r in caplog.records
    )


def test_no_tag_outside_scope(caplog: pytest.LogCaptureFixture) -> None:
    _logging.install_run_id_logging()
    logger = logging.getLogger("test.aqa.logging.none")
    with caplog.at_level(logging.INFO):
        logger.info("plain")
    assert any(r.getMessage() == "plain" for r in caplog.records)


def test_install_is_idempotent() -> None:
    _logging.install_run_id_logging()
    factory_after_first = logging.getLogRecordFactory()
    _logging.install_run_id_logging()
    # A second install must not re-wrap the factory (would double-tag).
    assert logging.getLogRecordFactory() is factory_after_first


def test_run_id_scope_resets_after_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _logging.install_run_id_logging()
    logger = logging.getLogger("test.aqa.logging.reset")
    with caplog.at_level(logging.INFO):
        with _logging.tag_logs_with_run_id("r1"):
            logger.info("inside")
        logger.info("outside")
    inside = next(r for r in caplog.records if "inside" in r.getMessage())
    outside = next(r for r in caplog.records if "outside" in r.getMessage())
    assert inside.getMessage().startswith("[run_id=r1 mem=")
    assert not outside.getMessage().startswith("[run_id=")


def test_logged_node_logs_start_and_done(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @_logging.log_node_run("my_node")
    def fn(ctx: object) -> str:
        return "ok"

    with caplog.at_level(logging.INFO):
        assert fn(object()) == "ok"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "node 'my_node' start" in messages
    assert "node 'my_node' done" in messages


def test_logged_node_logs_failure_and_reraises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @_logging.log_node_run("bad_node")
    def fn(ctx: object) -> None:
        raise ValueError("boom")

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError, match="boom"):
            fn(object())
    assert any(
        "node 'bad_node' failed" in r.getMessage() for r in caplog.records
    )
