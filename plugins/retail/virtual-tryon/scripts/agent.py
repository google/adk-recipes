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

"""Canonical ADK agent entrypoint for the retail virtual try-on plugin.

Re-exports ``root_agent`` and ``app`` from :mod:`scripts.tryon_agent` so ADK,
Gemini Enterprise Agent Platform, A2A serving (`fast_api_app.py`), and
`agents-cli` discover ``scripts.agent`` using the standard recipe layout.
"""

from scripts import tryon_agent

INSTRUCTION = tryon_agent.INSTRUCTION
try_on_product_image = tryon_agent.try_on_product_image
try_on_product_video = tryon_agent.try_on_product_video
root_agent = tryon_agent.root_agent
app = tryon_agent.app
