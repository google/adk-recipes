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

from .deep_research.agent import (
    deep_research_agent_tool,
)
from .deep_research.agent import (
    research_agent as deep_research_agent,
)
from .google_research.agent import (
    google_research_tool,
)
from .google_research.agent import (
    research_agent as google_research_agent,
)
from .rag.agent import internal_knowledge_search_tool, rag_agent
from .synthesizer.agent import (
    batch_slide_writer_tool,
    generate_outline_and_save_tool,
    outline_specialist_agent,
    outline_specialist_tool,
    slide_writer_agent,
    slide_writer_specialist_tool,
)

__all__ = [
    "batch_slide_writer_tool",
    "deep_research_agent",
    "deep_research_agent_tool",
    "generate_outline_and_save_tool",
    "google_research_agent",
    "google_research_tool",
    "internal_knowledge_search_tool",
    "outline_specialist_agent",
    "outline_specialist_tool",
    "rag_agent",
    "slide_writer_agent",
    "slide_writer_specialist_tool",
]
