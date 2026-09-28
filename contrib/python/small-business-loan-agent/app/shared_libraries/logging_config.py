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

"""Centralized logging configuration."""

import logging
import sys

_logging_state = {"configured": False}


def _setup_logging() -> None:
    """Set up basic logging configuration."""
    if _logging_state["configured"]:
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    for noisy_logger in [
        "httpx",
        "google.auth",
        "urllib3",
        "google_genai.models",
        "google_genai.types",
    ]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    _logging_state["configured"] = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with proper configuration."""
    if not _logging_state["configured"]:
        _setup_logging()

    return logging.getLogger(name)
