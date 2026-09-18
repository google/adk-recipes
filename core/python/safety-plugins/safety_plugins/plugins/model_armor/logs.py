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

"""Emit Model Armor metadata through Python's standard logging API."""

import logging

from .constants import (
    LOGGER_NAME,
    MODEL_ARMOR_UNAVAILABLE_REASON,
    ModelArmorAction,
    ModelArmorMethod,
    ModelArmorResponse,
    ScreeningStage,
)
from .telemetry import screening_fields

logger = logging.getLogger(LOGGER_NAME)


def record_screening(
    stage: ScreeningStage,
    response: ModelArmorResponse | None,
    *,
    action: ModelArmorAction,
    reasons: tuple[str, ...] = (),
    tool_name: str | None = None,
    error: Exception | None = None,
    method: ModelArmorMethod | None = None,
) -> None:
    fields = screening_fields(
        response, action=action, reasons=reasons, error=error
    )
    fields["stage"] = stage.value
    if method is not None:
        fields["method"] = method.value
    if tool_name is not None:
        fields["tool"] = tool_name
    logger.log(
        logging.WARNING if action == ModelArmorAction.BLOCK else logging.INFO,
        "Model Armor: stage=%s action=%s",
        stage.value,
        action.value,
        extra={"model_armor": fields},
    )


def record_screening_error(
    *,
    stage: ScreeningStage,
    method: ModelArmorMethod,
    error: Exception,
    response: ModelArmorResponse | None,
    tool_name: str | None = None,
) -> None:
    record_screening(
        stage,
        response,
        action=ModelArmorAction.BLOCK,
        reasons=(MODEL_ARMOR_UNAVAILABLE_REASON,),
        tool_name=tool_name,
        error=error,
        method=method,
    )


def log_client_initialised(*, project_id: str, location_id: str) -> None:
    logger.info(
        "Model Armor client initialized: project=%s region=%s",
        project_id,
        location_id,
    )
