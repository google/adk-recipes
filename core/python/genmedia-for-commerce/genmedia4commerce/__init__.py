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

"""GenMedia for Commerce package."""

import importlib
from pathlib import Path

from dotenv import load_dotenv

_CONFIG_ENV = Path(__file__).parents[1] / "config.env"
if _CONFIG_ENV.exists():
    load_dotenv(_CONFIG_ENV)


def __getattr__(name: str):
    """``genmedia4commerce.agent`` on first use, for the ADK tools.

    ``adk eval ./genmedia4commerce`` and ``adk web .`` load the agent as an
    attribute of this package from the repo root; importing it lazily keeps
    ``import genmedia4commerce`` light. (Pointing them at the agent's own
    folder would put ``genmedia4commerce/`` on ``sys.path``, where our
    ``requests.py``, ``tools.py`` and ``mcp/`` shadow those libraries.)
    """
    if name == "agent":
        return importlib.import_module("genmedia4commerce.agent")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
