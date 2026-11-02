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

"""Where ADK finds the app's agent: the Studio agent (``studio/agent.py``).

``adk web .`` and ``adk eval ./genmedia4commerce`` read ``root_agent`` and
``app`` from ``genmedia4commerce.agent``; the FastAPI app serves ``app``
through the ADK API.
"""

from genmedia4commerce.studio.agent import app, root_agent

__all__ = ["app", "root_agent"]
