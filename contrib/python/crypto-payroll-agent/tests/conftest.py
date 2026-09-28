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

"""Provide required environment variables for offline test runs.

The values are not duplicated here: every variable declared in the
recipe's `.env.example` is loaded from it, so that file stays the single
source of truth. Some of its values are `<TODO: update-this-value>`
placeholders — that is fine, and deliberate: the offline suite never
reaches a real credential, so a placeholder proves the tests do not
quietly depend on one. `load_dotenv` does not override, so a real
`.env` or an exported variable always wins.
"""

from pathlib import Path

from dotenv import load_dotenv

RECIPE_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(RECIPE_ROOT / ".env.example")
