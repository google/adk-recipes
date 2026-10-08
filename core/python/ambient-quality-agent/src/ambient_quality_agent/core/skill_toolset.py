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

"""Combined ADK SkillToolset for the chat agent.

Gemini platform rejects requests with duplicate tool declarations. Because each ADK
`SkillToolset` registers identical function names (`list_skills`, `load_skill`,
`load_skill_resource`), all skills are combined into a single toolset.
"""

from __future__ import annotations

from ambient_quality_agent.core.custom_investigation_skill import (
    load_custom_investigation_skill,
)
from ambient_quality_agent.core.rca_skill import load_rca_skill
from google.adk.tools.skill_toolset import SkillToolset

SKILL_TOOLS = ["list_skills", "load_skill", "load_skill_resource"]
"""Allowlist of skill tools exposed to the agent.

Listing the tools explicitly keeps a newly added upstream ADK skill tool from
reaching the agent without review. Excludes execution tools like
`run_skill_script` because registered skills are read-only procedures.
"""


def build_skill_toolset() -> SkillToolset:
    """Constructs the combined SkillToolset for the chat agent.

    Neither skill depends on configuration: `describe_telemetry` reports the
    telemetry source and selector table per request, so the toolset is the
    same for every attached agent.

    Returns:
        SkillToolset containing the RCA and custom investigation skills,
        filtered to `SKILL_TOOLS`.
    """
    return SkillToolset(
        skills=[load_rca_skill(), load_custom_investigation_skill()],
        tool_filter=SKILL_TOOLS,
    )
