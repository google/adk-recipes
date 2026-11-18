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

"""The Gemini Enterprise panel shows the server's default variations."""

import re
from pathlib import Path

from genmedia4commerce.studio import patches

PANEL = Path(patches.__file__).parents[1] / "mcp" / "static" / "studio.html"


def test_the_panel_defaults_match_the_server() -> None:
    text = PANEL.read_text()
    shown = dict(re.findall(r'(\w+): \{ [^\n]*?num_variations: "(\d)"', text))
    assert shown == patches.VARIATIONS
    assert patches.VARIATIONS == {"tryon": "3", "video_tryon": "3", "spin": "1"}
