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
"""Single sink for feedback records — human button and agent self-report alike."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_cloud_logger: Any = None


def _build_cloud_logger() -> Any:
    from google.cloud import logging as google_cloud_logging

    return google_cloud_logging.Client().logger("app.feedback")


def _get_cloud_logger() -> Any:
    global _cloud_logger
    if _cloud_logger is None:
        _cloud_logger = _build_cloud_logger()
    return _cloud_logger


def emit_feedback_record(record: dict[str, Any]) -> None:
    try:
        _get_cloud_logger().log_struct(record, severity="INFO")
        logger.debug(
            "feedback: emitted via cloud logging (origin=%s)",
            record.get("origin"),
        )
    except Exception:
        # Cloud Logging sink is down — fall back to the stdlib logger so the
        # full record still reaches Cloud Logging via the OTEL log handler
        # rather than vanishing. emit_feedback_record must never raise.
        logger.exception(
            "feedback: cloud-logging sink failed; falling back to stdlib log"
        )
        logger.error("feedback record (cloud-logging sink failed): %s", record)
