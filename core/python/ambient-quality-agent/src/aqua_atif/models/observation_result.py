# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/observation_result.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Observation result model for ATIF trajectories."""

from typing import Any

from aqua_atif.models.content import ContentPart
from aqua_atif.models.subagent_trajectory_ref import SubagentTrajectoryRef
from pydantic import BaseModel, Field


class ObservationResult(BaseModel):
    """A single result within an observation."""

    source_call_id: str | None = Field(
        default=None,
        description=(
            "The `tool_call_id` from the _tool_calls_ array in _StepObject_ that this "
            "result corresponds to. If null or omitted, the result comes from an "
            "action that doesn't use the standard tool calling format (e.g., agent "
            "actions without tool calls or system-initiated operations)."
        ),
    )
    content: str | list[ContentPart] | None = Field(
        default=None,
        description=(
            "The output or result from the tool execution. String for text-only "
            "content, or array of ContentPart for multimodal content (added in ATIF-v1.6)."
        ),
    )
    subagent_trajectory_ref: list[SubagentTrajectoryRef] | None = Field(
        default=None,
        description="Array of references to delegated subagent trajectories",
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Custom observation-result-level metadata (e.g., confidence score, "
            "retrieval score, source document ID). Added in ATIF-v1.7."
        ),
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}
