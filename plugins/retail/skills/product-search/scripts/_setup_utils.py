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

"""Shared utilities for skill setup scripts.

Provides load_config() for reading YAML frontmatter from design-spec.md.

Vendored locally so this skill ships as a self-contained package -- no
parent-directory _shared/ folder required to run setup.
"""

import logging
import pathlib
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict[str, Any]:
    """Load design-spec.md YAML frontmatter.

    Args:
        config_path: Path to the design-spec markdown file.

    Returns:
        Parsed YAML frontmatter as a dict. Empty dict if the file is
        missing, has no frontmatter, or the frontmatter is empty.
    """
    if not pathlib.Path(config_path).exists():
        return {}
    text = pathlib.Path(config_path).read_text(encoding="utf-8")
    if text.startswith("---"):
        lines = text.split("\n")
        yaml_lines = []
        in_frontmatter = False
        for line in lines:
            if line.strip() == "---":
                if not in_frontmatter:
                    in_frontmatter = True
                    continue
                break
            if in_frontmatter:
                yaml_lines.append(line)
        yaml_text = "\n".join(yaml_lines)
        return yaml.safe_load(yaml_text) or {}
    return yaml.safe_load(text) or {}
