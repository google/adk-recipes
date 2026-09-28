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

"""Web search via ADK's ``google_search`` built-in (Path A).

Re-exports ADK's ``google_search`` singleton so consumers can import it
from the same ``app.tools.*`` surface as our other tools. Path A means
the tool is a model-side built-in — Gemini's native search-grounding
fires when the singleton is registered on an Agent's ``tools`` list,
without any local HTTP client or provider plugin layer.

Earlier iterations shipped a large multi-provider web-search registry
(DuckDuckGo / SearXNG / Exa / Tavily); this drops the whole abstraction
in favor of native grounding.
"""

from google.adk.tools import google_search

__all__ = ["google_search"]
