# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/final_metrics.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Final metrics model for ATIF trajectories."""

from typing import Any

from pydantic import BaseModel, Field


class FinalMetrics(BaseModel):
    """Aggregate statistics for the entire trajectory."""

    total_prompt_tokens: int | None = Field(
        default=None,
        description="Sum of all prompt tokens across all steps, including cached tokens",
    )
    total_completion_tokens: int | None = Field(
        default=None,
        description="Sum of all completion tokens across all steps",
    )
    total_cached_tokens: int | None = Field(
        default=None,
        description="Sum of all cached tokens across all steps",
    )
    total_cost_usd: float | None = Field(
        default=None,
        description="Total real monetary cost for the entire trajectory, including cost for subagents, if any",
    )
    total_steps: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Total number of steps. If not equivalent to the number of steps in the "
            "trajectory, must be documented in the root-level notes field."
        ),
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description="Custom aggregate metrics",
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}
