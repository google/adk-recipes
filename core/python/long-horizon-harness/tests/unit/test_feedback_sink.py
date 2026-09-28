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

"""Feedback sink — the single path that writes a feedback record to Cloud Logging.

Both the human feedback button (HTTP ``/feedback``) and the agent self-report
tool route through ``emit_feedback_record`` so the two sources land identically.
The Cloud Logging client is built once and reused.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from app.feedback import sink


def test_emit_logs_struct_unchanged(monkeypatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(sink, "_get_cloud_logger", lambda: fake)
    record = {"log_type": "feedback", "origin": "agent", "text": "hi"}

    sink.emit_feedback_record(record)

    fake.log_struct.assert_called_once_with(record, severity="INFO")


def test_cloud_logger_built_once(monkeypatch) -> None:
    monkeypatch.setattr(sink, "_cloud_logger", None)
    builder = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(sink, "_build_cloud_logger", builder)

    sink.emit_feedback_record({"a": 1})
    sink.emit_feedback_record({"a": 2})

    assert builder.call_count == 1


def test_emit_falls_back_to_stdlib_logger_when_sink_raises(
    monkeypatch, caplog
) -> None:
    boom = MagicMock()
    boom.log_struct.side_effect = RuntimeError("quota exceeded")
    monkeypatch.setattr(sink, "_get_cloud_logger", lambda: boom)
    record = {
        "log_type": "feedback",
        "origin": "user",
        "text": "please add dark mode",
    }

    with caplog.at_level(logging.ERROR, logger=sink.logger.name):
        sink.emit_feedback_record(record)  # must NOT raise

    # The full record is serialized into the stdlib log stream as a fallback.
    assert any("please add dark mode" in r.getMessage() for r in caplog.records)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_emit_does_not_propagate_when_logger_build_fails(monkeypatch) -> None:
    monkeypatch.setattr(sink, "_cloud_logger", None)
    monkeypatch.setattr(
        sink,
        "_build_cloud_logger",
        MagicMock(side_effect=RuntimeError("no creds")),
    )
    # Should swallow the build error and fall back, not raise.
    sink.emit_feedback_record(
        {"log_type": "feedback", "origin": "user", "text": "hi"}
    )


def test_emit_logs_success_breadcrumb(monkeypatch, caplog) -> None:
    fake = MagicMock()
    monkeypatch.setattr(sink, "_get_cloud_logger", lambda: fake)

    with caplog.at_level(logging.DEBUG, logger=sink.logger.name):
        sink.emit_feedback_record({"origin": "agent", "text": "hi"})

    fake.log_struct.assert_called_once()
    assert any("emitted" in r.getMessage() for r in caplog.records)
