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

"""High-Volume Document Analyzer Agent: query and synthesize information from documents."""

import os
from pathlib import Path

import google.auth
from dotenv import load_dotenv

# Load variables from .env if present, falling back to .env.example for defaults.
load_dotenv()
load_dotenv(Path(__file__).resolve().parent.parent / ".env.example")

try:
    _, project_id = google.auth.default()
except Exception:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")

if project_id and (
    "GOOGLE_CLOUD_PROJECT" not in os.environ
    or os.environ["GOOGLE_CLOUD_PROJECT"].startswith("<TODO")
):
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id

from . import agent  # noqa: E402 -- must come after load_dotenv()
