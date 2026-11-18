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

import os

from dotenv import load_dotenv

# Load variables from .env if present. In production the environment is
# already populated by the platform (Cloud Run, GKE, etc.), so a missing
# .env is expected and not an error.
load_dotenv()

# .env.example declares these optional serving variables with empty values.
# Some libraries treat an empty value as set (Agent Engine's AdkApp would try
# to use GOOGLE_CLOUD_AGENT_ENGINE_ID=""), so drop the empty ones.
_OPTIONAL_SERVING_VARS = (
    "ALLOW_ORIGINS",
    "GOOGLE_CLOUD_AGENT_ENGINE_ID",
    "GOOGLE_CLOUD_PROJECT",
    "INTEGRATION_TEST",
    "LOGS_BUCKET_NAME",
    "SESSION_SERVICE_URI",
)
for _name in _OPTIONAL_SERVING_VARS:
    if _name in os.environ and not os.environ[_name]:
        del os.environ[_name]

from .agent import app  # noqa: E402 -- must come after load_dotenv()

__all__ = ["app"]
