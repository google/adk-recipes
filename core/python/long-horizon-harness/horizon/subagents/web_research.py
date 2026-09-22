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

"""Web-research sub-agent — isolates ``google_search`` from the root agent's tool set.

Gemini's API rejects any request that combines ``google_search`` with
non-search function tools::

    "Multiple tools are supported only when they are all search tools."

The root agent ships ~10 function tools (file_ops, bash,
memory, ...), so ``google_search`` cannot live alongside them. The
documented escape hatch is the sub-agent split: a dedicated agent whose
``tools=[]`` is JUST the search tool, surfaced to the root agent through
``AgentTool``. The root agent calls the sub-agent like a function;
the sub-agent makes a search-only API call internally.

The surfaced tool name is ``web_research`` — naming it ``search_agent``
made the model reach for ``bash``-based scraping instead, because
"search" reads as filesystem/memory search.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.models import Gemini

from horizon.models.registry import ROBUST_RETRY_OPTIONS
from horizon.tools.web_search import google_search

web_research_agent = Agent(
    name="web_research",
    model=Gemini(
        model="gemini-3.7-flash",
        retry_options=ROBUST_RETRY_OPTIONS,
    ),
    # AgentTool surfaces THIS to the root model, not `instruction` (which
    # only the sub-agent itself reads) -- left unset this tool description
    # was empty and the root model chose blind.
    description=(
        "Search the live web for current information: news, docs, prices, "
        "anything outside the workspace or your training data. Returns a "
        "concise, sourced summary. To fetch one known URL, use bash curl."
    ),
    instruction=(
        "You perform web searches and return concise, sourced results. "
        "Call google_search exactly once per query. Cite sources inline."
    ),
    tools=[google_search],
)
