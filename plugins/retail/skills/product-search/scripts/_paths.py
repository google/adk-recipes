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

"""Install-dir resolution for the retail-product-search skill.

INSTALL_DIR is the directory containing SKILL.md -- i.e. the root of the
installed skill, regardless of where the user invokes a script from.
Computed from this file's own location at import time, so it stays stable
even when the user's cwd changes mid-session.

Use this for any "where do I find my bundled assets?" lookup.
Do NOT use this for "where should I write user state?" -- user-mutable
state goes to the agent's current working directory.
"""

from __future__ import annotations

import pathlib

INSTALL_DIR: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent

ASSETS_DIR: pathlib.Path = INSTALL_DIR / "assets"
SAMPLE_PRODUCTS_CSV: pathlib.Path = ASSETS_DIR / "sample-products.csv"
DEFAULT_DESIGN_SPEC: pathlib.Path = ASSETS_DIR / "design-spec.md"
