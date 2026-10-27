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

"""Typed models for Cloud Trace spans and log entries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SpanEvent(BaseModel):
    """An OpenTelemetry span event."""

    name: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanStatus(BaseModel):
    """An OpenTelemetry span status."""

    code: int | None = None
    message: str | None = None


class Span(BaseModel):
    """One agent trace span from the ``_AllSpans`` linked dataset."""

    span_id: str = ""
    parent_span_id: str | None = None
    name: str | None = None
    end_time: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: SpanStatus = Field(default_factory=SpanStatus)
    events: list[SpanEvent] = Field(default_factory=list)


class LogEntryView(BaseModel):
    """The fields of a Cloud Logging entry the converter consumes.

    The ``labels`` map (carrying ``event.name`` and any ``*_ref`` / inline
    attribute labels) and the ``jsonPayload`` struct.
    """

    labels: dict[str, str] = Field(default_factory=dict)
    json_payload: dict[str, Any] = Field(default_factory=dict)
