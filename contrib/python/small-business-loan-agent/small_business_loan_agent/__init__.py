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

"""Small Business Loan Processing Agent — ADK reference implementation."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Ensure the parent directory is on sys.path so the package is importable
# (required for `adk eval` which doesn't set up sys.path like `adk web` does)
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

try:
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        import google.auth

        _, project_id = google.auth.default()
        if project_id:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
except Exception:
    pass

if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
if not os.environ.get("MODEL_NAME"):
    os.environ["MODEL_NAME"] = "gemini-3.8-flash"
if not os.environ.get("JUDGE_MODEL"):
    os.environ["JUDGE_MODEL"] = os.environ["MODEL_NAME"]

from small_business_loan_agent import agent  # noqa: E402
