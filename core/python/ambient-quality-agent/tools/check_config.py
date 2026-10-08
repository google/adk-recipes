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

"""Check the agent's configuration, and say what is wrong in one line.

Loads `src/ambient_quality_agent/.env` the way `tools/local_ui.sh` hands it to
uvicorn, then builds the configuration the agent would start with. Exits 0 when
it builds, and 1 with the error message when it does not.

It sits outside the package on purpose. Importing any part of
`ambient_quality_agent` loads the configuration, so a check inside the package
would fail before it ran, with the traceback this exists to avoid.
"""

from __future__ import annotations

import logging
import pathlib
import sys

from dotenv import load_dotenv

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / "src/ambient_quality_agent/.env"


def main() -> int:
    """Builds the agent's configuration and reports whether it is valid.

    Returns:
        0 when the configuration builds, 1 when it does not.
    """
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE)
    # Only the verdict matters here. The agent logs its own warnings when it
    # starts, and saying them twice would bury them.
    logging.disable(logging.CRITICAL)
    try:
        import ambient_quality_agent.config  # noqa: F401
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if not ENV_FILE.is_file():
            print(
                f"There is no {ENV_FILE.relative_to(REPO_ROOT)} either; "
                "see .env.example.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
