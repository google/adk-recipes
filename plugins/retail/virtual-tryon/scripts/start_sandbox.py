#!/usr/bin/env python3
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

"""FastAPI uvicorn launcher.

Runs the VTO catalog sandbox uvicorn server on localhost. Config comes from
`.env` (loaded via scripts.config) — for Q-MODE users, `setup.py` writes
`.env` from `design-spec.md` before this script runs.
"""

import argparse
import logging
import sys
from pathlib import Path

# Add parent of scripts directory to path to enable package resolution.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="./design-spec.md",
        help=(
            "Path to design-spec.md. Kept for CLI compatibility; runtime "
            "config now comes from .env (populated by setup.py)."
        ),
    )
    args = parser.parse_args()

    # Sanity check: warn loudly if design-spec.md doesn't exist AND .env
    # doesn't either. Q-MODE users hit this only if they skipped setup.py.
    workspace = Path(args.config).resolve().parent
    if not (workspace / ".env").exists() and not Path(args.config).exists():
        logger.warning(
            "Neither .env nor %s was found in %s. "
            "The sandbox will start using scripts.config defaults, which "
            "requires GOOGLE_CLOUD_PROJECT to be set in the ambient environment.",
            args.config,
            workspace,
        )

    if not config.GOOGLE_CLOUD_PROJECT:
        logger.error(
            "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and "
            "fill it in, or run scripts/setup.py --config ./design-spec.md."
        )
        sys.exit(1)

    import uvicorn

    logger.info("=" * 60)
    logger.info("Starting Virtual Try-On Sandbox Dashboard")
    logger.info(f"  Project:    {config.GOOGLE_CLOUD_PROJECT}")
    logger.info(f"  Region:     {config.GCP_REGION}")
    logger.info(f"  Image model: {config.GEMINI_IMAGE_MODEL}")
    logger.info(f"Access at:   http://localhost:{config.PORT}")
    logger.info("=" * 60)

    uvicorn.run(
        "scripts.server:app", host="0.0.0.0", port=config.PORT, reload=True
    )
