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
"""Loads the documented defaults so tests run without a local .env."""

from pathlib import Path

from dotenv import load_dotenv

# Values already in the environment (or in .env, loaded by app/__init__.py)
# take precedence; .env.example fills in anything missing.
load_dotenv(
    Path(__file__).resolve().parents[1] / ".env.example", override=False
)
