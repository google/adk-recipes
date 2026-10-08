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

"""RCA skill loader for insight investigations.

Loads and caches the RCA skill defined in `SKILL.md` for consumption by
`ambient_quality_agent.core.skill_toolset`.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import TYPE_CHECKING

from google.adk.skills import load_skill_from_dir

if TYPE_CHECKING:
    from google.adk.skills import Skill

SKILL_DIR = Path(__file__).parent.parent / "skills" / "rca"
"""Path to the RCA skill directory.

Located inside the package because the root `skills/` directory is excluded by
`.gcloudignore` and omitted from deployed container images.
"""


@functools.cache
def load_rca_skill() -> Skill:
    """Loads and caches the RCA skill definition from `SKILL.md`.

    Cached because the skill definition is immutable at runtime and accessed
    frequently during chat agent initialization and testing.

    Returns:
        The loaded Skill instance.

    Raises:
        FileNotFoundError: If the skill directory is missing from the package.
        ValueError: If the skill frontmatter is malformed.
    """
    return load_skill_from_dir(SKILL_DIR)
