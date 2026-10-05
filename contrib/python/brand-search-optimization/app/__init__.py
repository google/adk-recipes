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

from pathlib import Path

from dotenv import load_dotenv

# Environment bootstrap for the whole package. Real values come from .env
# (gitignored) or the ambient environment. The committed .env.example is the
# fallback so the unit tests and the runnability test import cleanly with no
# .env present. override=False throughout, so a real environment variable wins.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=False)
load_dotenv(_ROOT / ".env.example", override=False)

from .agent import root_agent  # noqa: E402 -- must come after load_dotenv()

__all__ = ["root_agent"]
