"""Make this skill's scripts/ importable from its tests.

Mirrors the shim the sibling repo skills use, so the skill stays a
self-contained bundle rather than depending on repo-root pytest config.

NOTE: the root pytest config lists `.agents/skills` in testpaths and
tools-tests.yml fires on any `.agents/**` change, so CI runs these. A failure
here turns a PR red -- run them before pushing:
  uv run pytest .agents/skills/github-pr-review/tests
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
