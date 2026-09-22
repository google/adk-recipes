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

Provides load_config() for reading YAML frontmatter from design-spec.md
and run_step() for executing pipeline commands.
"""

import logging
import subprocess
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """Load design-spec.md YAML frontmatter."""
    if not Path(config_path).exists():
        return {}
    text = Path(config_path).read_text()
    if text.startswith("---"):
        lines = text.split("\n")
        yaml_lines = []
        in_frontmatter = False
        for line in lines:
            if line.strip() == "---":
                if not in_frontmatter:
                    in_frontmatter = True
                    continue
                else:
                    break
            if in_frontmatter:
                yaml_lines.append(line)
        yaml_text = "\n".join(yaml_lines)
        return yaml.safe_load(yaml_text) or {}
    return yaml.safe_load(text) or {}


def run_step(description: str, cmd: list, dry_run: bool = False) -> bool:
    """Run a pipeline step. Returns True on success."""
    logger.info(f"\n  {description}")
    if dry_run:
        logger.info(f"    [dry-run] {' '.join(cmd)}")
        return True
    logger.info(f"    Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, check=False)
    if result.returncode != 0:
        logger.error(f"    FAILED (exit code {result.returncode})")
        return False
    return True
