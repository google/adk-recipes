# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/observation.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Observation model for ATIF trajectories."""

from aqua_atif.models.observation_result import ObservationResult
from pydantic import BaseModel, Field


class Observation(BaseModel):
    """Environment feedback/result after actions or system events."""

    results: list[ObservationResult] = Field(
        default=...,
        description="Array of result objects from tool calls or actions",
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}
