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

"""Verifies that local `.env` files do not leak into the test suite.

Importing `ambient_quality_agent` invokes `load_dotenv()`. `conftest.py`
disables dotenv loading to maintain test isolation across the process.
"""

from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv


def test_loading_a_dotenv_file_changes_nothing(tmp_path: pathlib.Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AQA_DOTENV_GUARD_PROBE=leaked\n", encoding="utf-8")

    assert load_dotenv(env_file) is False
    assert "AQA_DOTENV_GUARD_PROBE" not in os.environ
