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
"""Environment bootstrap for the unit suite.

``app/agent.py`` builds the agents at import time, which needs
``MODEL_NAME``. pytest loads this conftest before it collects sibling test
modules, so seeding the environment here happens before any ``import app``.

The values mirror ``.env.example``. ``setdefault`` keeps a developer's real
``.env`` settings; nothing in the unit suite reaches the network.
"""

import os

os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "false")
os.environ.setdefault("GOOGLE_API_KEY", "test-key-never-used")
os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key-never-used")
