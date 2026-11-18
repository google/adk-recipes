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

"""Telemetry setup hints."""

import logging
import os

from google.adk.telemetry import context

from genmedia4commerce.app_utils.telemetry import setup_telemetry

CONTENT_IN_SPANS = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"


def test_disabled_hint_names_a_bare_bucket(monkeypatch, caplog):
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)
    with caplog.at_level(logging.INFO):
        assert setup_telemetry() is None
    assert "LOGS_BUCKET_NAME=your-bucket " in caplog.text
    assert "gs://" not in caplog.text


def test_adk_spans_leave_message_content_out_by_default(monkeypatch):
    monkeypatch.setenv(CONTENT_IN_SPANS, "")  # restored after the test
    monkeypatch.delenv(CONTENT_IN_SPANS)
    assert context._read_add_content_to_legacy_spans()  # ADK's default

    setup_telemetry()

    assert os.environ[CONTENT_IN_SPANS] == "false"
    assert not context._read_add_content_to_legacy_spans()


def test_adk_span_content_can_be_turned_back_on(monkeypatch):
    monkeypatch.setenv(CONTENT_IN_SPANS, "true")

    setup_telemetry()

    assert context._read_add_content_to_legacy_spans()
