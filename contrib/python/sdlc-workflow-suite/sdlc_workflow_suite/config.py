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

import os

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """Configuration for the SDLC Workflow Suite agents."""

    model_name: str | None = Field(
        default_factory=lambda: os.getenv("MODEL_NAME"),
        validation_alias=AliasChoices(
            "MODEL_NAME", "AGENT_DEFAULT_LLM", "DEFAULT_LLM"
        ),
    )
    spanner_project_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SPANNER_PROJECT_ID", "AGENT_SPANNER_PROJECT_ID"
        ),
    )
    spanner_instance_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SPANNER_INSTANCE_ID", "AGENT_SPANNER_INSTANCE_ID"
        ),
    )
    spanner_database_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SPANNER_DATABASE_ID", "AGENT_SPANNER_DATABASE_ID"
        ),
    )

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        env_file=".env",
        env_file_encoding="utf-8",
        cli_parse_args=False,
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def default_llm(self) -> str | None:
        """Backward-compatible alias for model_name."""
        return self.model_name

    @property
    def is_spanner_configured(self) -> bool:
        """Returns True if all required Spanner settings are provided."""
        return bool(
            self.spanner_project_id
            and self.spanner_instance_id
            and self.spanner_database_id
        )


config = AgentConfig()
