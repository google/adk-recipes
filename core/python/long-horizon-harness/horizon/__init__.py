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

"""Horizon — a reference long-horizon ADK agent.

- The agent: ``horizon.agent`` (``root_agent`` / ``app``).
- Served over HTTP: ``horizon.fast_api_app:app`` (``uvicorn horizon.fast_api_app:app``).
- Embedded headless: ``horizon.fast_api_app.build_runner()`` → an ADK ``Runner``.

Configure via environment variables (see ``docs/configuration.md``); adapt by
editing the code — this is a recipe, not a framework.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Distribution name differs from the import package ("horizon").
    __version__ = _pkg_version("long-horizon-harness")
except (
    PackageNotFoundError
):  # editable/source checkout without installed dist metadata
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
