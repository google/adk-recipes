# Copyright 2025 Google LLC
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

"""Google Trends agent: surface realtime Google Trends via BigQuery."""

import os

import google.auth
from dotenv import load_dotenv

# Load variables from .env if present. In production the environment is
# already populated by the platform (Cloud Run, Agent Engine, etc.), so a
# missing .env is expected and not an error.
load_dotenv()

from . import agent  # noqa: E402 -- must come after load_dotenv()

_, project_id = google.auth.default()
# google.auth.default() returns no project when it cannot infer one (for
# example ADC with no associated project). Guard it: os.environ.setdefault
# requires a str and would otherwise raise TypeError at import time.
if project_id:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
# gemini-3.x models are served on the "global" endpoint, so force it here.
# This governs MODEL calls only; Agent Engine hosting region is set separately
# in deployment/deploy.py via AGENT_ENGINE_LOCATION.
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
