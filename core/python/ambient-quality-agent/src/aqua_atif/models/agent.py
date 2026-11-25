# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/agent.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Agent configuration model for ATIF trajectories."""

from typing import Any

from pydantic import BaseModel, Field


class Agent(BaseModel):
    """Agent configuration."""

    name: str = Field(
        default=...,
        description="The name of the agent system",
    )
    version: str = Field(
        default=...,
        description="The version identifier of the agent system",
    )
    model_name: str | None = Field(
        default=None,
        description="Default LLM model used for this trajectory",
    )
    tool_definitions: list[dict[str, Any]] | None = Field(
        default=None,
        description="Array of tool/function definitions available to the agent. Each element follows OpenAI's function calling schema.",
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description="Custom agent configuration details",
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}
