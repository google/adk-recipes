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

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Ensure the parent directory is on sys.path so the package is importable
# (required for `adk eval` which doesn't set up sys.path like `adk web` does)
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Best-effort ADC lookup: importing the package must not hard-fail when no
# credentials are configured (e.g. unit tests, `uv run pytest` in CI), but the
# failure is surfaced instead of being swallowed.
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        import google.auth
        from google.auth.exceptions import GoogleAuthError

        _, project_id = google.auth.default()
        if project_id:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        else:
            logger.warning(
                "Application Default Credentials resolved without a project; "
                "set GOOGLE_CLOUD_PROJECT in your .env file."
            )
    except (GoogleAuthError, ImportError) as e:
        logger.warning(
            "Could not resolve GOOGLE_CLOUD_PROJECT from Application Default "
            "Credentials (%s). Set it explicitly in your .env file.",
            e,
        )

if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
# MODEL_NAME deliberately has no in-code fallback: it is declared in
# .env.example and loaded by load_dotenv() above, so the model is chosen
# without a hardcoded literal in the source.
#
# Fail fast and say why. Without this guard a missing MODEL_NAME travels as
# None into GeminiPreview(model=None) and surfaces as a pydantic
# "Input should be a valid string" deep inside a sub-agent import, which says
# nothing about the actual problem (no .env file).
_model_name = os.environ.get("MODEL_NAME")
if not _model_name:
    raise RuntimeError(
        "MODEL_NAME is not set. Copy .env.example to .env in the recipe root "
        "(cp .env.example .env) and set MODEL_NAME, or export it in your "
        "environment."
    )

# The judge mirrors the main model unless the operator overrides it separately.
if not os.environ.get("JUDGE_MODEL"):
    os.environ["JUDGE_MODEL"] = _model_name

from small_business_loan_agent import agent  # noqa: E402
