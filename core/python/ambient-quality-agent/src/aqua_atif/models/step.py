# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/step.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Step model for ATIF trajectories."""

from datetime import datetime
from typing import Any, Literal

from aqua_atif.models.content import ContentPart
from aqua_atif.models.metrics import Metrics
from aqua_atif.models.observation import Observation
from aqua_atif.models.tool_call import ToolCall
from pydantic import BaseModel, Field, field_validator, model_validator


class Step(BaseModel):
    """A single step in the trajectory."""

    step_id: int = Field(
        default=...,
        ge=1,
        description="Ordinal index of the turn (starting from 1)",
    )
    timestamp: str | None = Field(
        default=None,
        description="ISO 8601 timestamp indicating when this step occurred",
    )
    source: Literal["system", "user", "agent"] = Field(
        default=...,
        description="The originator of this step",
    )
    model_name: str | None = Field(
        default=None,
        description=(
            "The specific LLM model used for this turn. Omission implies the model "
            "defined in the root-level agent config."
        ),
    )
    reasoning_effort: str | float | None = Field(
        default=None,
        description="Qualitative or quantitative measure of effort",
    )
    message: str | list[ContentPart] = Field(
        default=...,
        description=(
            "The dialogue message. String for text-only content, or array of "
            "ContentPart for multimodal content (added in ATIF-v1.6)."
        ),
    )
    reasoning_content: str | None = Field(
        default=None,
        description="The agent's explicit internal reasoning",
    )
    tool_calls: list[ToolCall] | None = Field(
        default=None,
        description="Array of structured objects for the agent's actions",
    )
    observation: Observation | None = Field(
        default=None,
        description="Environment feedback/result after actions or system events",
    )
    metrics: Metrics | None = Field(
        default=None,
        description="LLM operational and confidence data for this step",
    )
    is_copied_context: bool | None = Field(
        default=None,
        description=(
            "Indicates whether this step was copied from a previous trajectory "
            "for context (e.g., during continuation after summarization). "
            "Steps marked as copied context should not be included in training data "
            "as they represent previously-trained interactions. "
            "Added in ATIF-v1.5."
        ),
    )
    llm_call_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Number of LLM inferences this step represents. When >1, metrics are "
            "aggregated across multiple LLM calls. When 1, the step represents exactly "
            "one inference. When 0 on a `source: 'agent'` step, the step represents a "
            "deterministic (non-LLM) dispatch; `metrics` and `reasoning_content` MUST "
            "be absent in that case. When null, the producer did not track this "
            "(backward-compatible default). Added in ATIF-v1.7."
        ),
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description="Custom step-level metadata",
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str | None) -> str | None:
        """Validate that timestamp is a valid ISO 8601 string."""
        if v is not None:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError as e:
                raise ValueError(f"Invalid ISO 8601 timestamp: {e}")  # noqa: B904 - kept as vendored
        return v

    @model_validator(mode="after")
    def validate_agent_only_fields(self) -> "Step":
        """Validate that certain fields are only present for agent steps."""
        if self.source != "agent":
            agent_only_fields = [
                "model_name",
                "reasoning_effort",
                "reasoning_content",
                "tool_calls",
                "metrics",
            ]
            for field in agent_only_fields:
                if getattr(self, field) is not None:
                    raise ValueError(
                        f"Field '{field}' is only applicable when source is 'agent', "
                        f"but source is '{self.source}'"
                    )
        return self

    @model_validator(mode="after")
    def validate_llm_call_count_zero_fields(self) -> "Step":
        """Enforce ATIF v1.7 no-LLM orchestration rule.

        When ``llm_call_count == 0`` on a ``source: "agent"`` step, the step
        represents a deterministic (non-LLM) dispatch. LLM-specific fields
        (``metrics``, ``reasoning_content``) MUST be absent on such steps.
        """
        if self.llm_call_count == 0 and self.source == "agent":
            llm_only_fields = ["metrics", "reasoning_content"]
            for field in llm_only_fields:
                if getattr(self, field) is not None:
                    raise ValueError(
                        f"Field '{field}' must be absent when llm_call_count is 0 "
                        f"(deterministic dispatch on a 'source: agent' step)"
                    )
        return self
