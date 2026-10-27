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

"""Custom investigation skill, shared by every telemetry source.

Guides the agent in writing SQL selectors, previewing matches, and scheduling
investigations. The skill is static: `SKILL.md` is source-neutral, and each
source's column notes and selector recipes ship as `references/<source>.md`.
The source and the selector table depend on the attached agent's configuration,
so `tools.telemetry.describe.describe_telemetry` resolves them per request and
points the agent at the reference to load. Exposed to the chat agent through
`ambient_quality_agent.core.skill_toolset`.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import TYPE_CHECKING

from ambient_quality_agent.config import ALLOWED_TELEMETRY_INGESTION_SOURCES
from google.adk.skills import load_skill_from_dir

if TYPE_CHECKING:
    from google.adk.skills import Skill

SKILL_NAME = "custom-investigation"
"""Skill name declared in the `SKILL.md` frontmatter."""

SKILL_DIR = Path(__file__).parent.parent / "skills" / SKILL_NAME
"""Path to the custom investigation skill directory.

Placed inside the package because `.gcloudignore` excludes the root `skills/`
directory from deployed container images. ADK requires the directory name to
match the skill name, which is kebab-case by ADK convention.
"""


@functools.cache
def load_custom_investigation_skill() -> Skill:
    """Loads the custom investigation skill with its per-source references.

    Cached to avoid re-reading the skill files on every agent construction; the
    content does not depend on configuration.

    Returns:
        Skill instance whose references hold one selector-recipe file per
        telemetry source.

    Raises:
        ValueError: If the `SKILL.md` frontmatter is invalid.
        FileNotFoundError: If the skill directory is missing.
    """
    return load_skill_from_dir(SKILL_DIR)


def resolve_selector_recipes(source: str) -> dict[str, str]:
    """Resolves the skill resource holding a telemetry source's selector recipes.

    Args:
        source: Telemetry source key (`big_query`, `cloud_ops`, or `cloud_logging`).

    Returns:
        The `skill_name` and `file_path` arguments that ADK's
        `load_skill_resource` tool takes to read the source's reference.

    Raises:
        ValueError: If `source` is unsupported.
    """
    if source not in ALLOWED_TELEMETRY_INGESTION_SOURCES:
        allowed = ", ".join(sorted(ALLOWED_TELEMETRY_INGESTION_SOURCES))
        raise ValueError(
            f"Unknown telemetry source {source!r}; expected one of {{{allowed}}}."
        )
    return {"skill_name": SKILL_NAME, "file_path": f"references/{source}.md"}
