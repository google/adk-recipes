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

"""Package init: load configuration before the agent module is imported."""

import os

from dotenv import load_dotenv

# Load variables from .env if present. In production the environment is
# already populated by the platform, so a missing .env is expected and not an
# error. This must run before `from . import agent`, so that env-var reads at
# agent-import time (e.g. MODEL_NAME) see the values from .env.
load_dotenv()


# Choose the auth backend. GOOGLE_GENAI_USE_VERTEXAI=1 uses Vertex AI (needs a
# project + location); anything else uses the Google AI (ML Developer) API
# with an API key. The relevant credential is validated up front so a
# misconfiguration fails with a clear message rather than deep inside a call.
def _is_unset(name: str) -> bool:
    """True if a variable is missing or still the .env.example placeholder."""
    value = os.getenv(name)
    return not value or value.startswith("<TODO")


_use_vertex_ai = os.getenv("GOOGLE_GENAI_USE_VERTEXAI") == "1"

if _use_vertex_ai:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
    if _is_unset("GOOGLE_CLOUD_PROJECT"):
        raise ValueError(
            "GOOGLE_CLOUD_PROJECT is not set. Set it in your .env file "
            "(see .env.example)."
        )
    if _is_unset("GOOGLE_CLOUD_LOCATION"):
        raise ValueError(
            "GOOGLE_CLOUD_LOCATION is not set. Set it in your .env file "
            "(see .env.example)."
        )
else:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
    if _is_unset("GOOGLE_API_KEY"):
        raise ValueError(
            "GOOGLE_API_KEY is not set. Get one from "
            "https://aistudio.google.com/app/apikey and set it in your .env "
            "file (see .env.example)."
        )

from . import agent  # noqa: E402 -- must come after configuration above
