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

"""Deep Search Agent: strategize, research, and synthesize comprehensive reports."""

import os

import google.auth
from dotenv import load_dotenv

# Load variables from .env if present. In production the environment is
# already populated by the platform (Cloud Run, GKE, etc.), so a missing
# .env is expected and not an error.
load_dotenv()

_, project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
# This entry-point is Vertex AI only; set explicitly so a stale env value
# cannot silently override the detected auth mode.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Env bootstrap above must run before importing the agent (which reads env
# vars at import time), so this import is intentionally not at the top.
from app.agent import root_agent  # noqa: E402
