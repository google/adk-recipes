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

# high_volume_document_analyzer/agent.py

"""High-Volume Document Analyzer Agent: query and synthesize information from documents."""

import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.apps import App

from high_volume_document_analyzer.prompt import ROOT_AGENT_INSTRUCTION
from high_volume_document_analyzer.tools.document_toolset import (
    analyze_document_next_chunk,
)

load_dotenv()

root_agent = LlmAgent(
    name="document_analyzer_agent",
    description="Agent that analyzes document collections in chunks to answer user questions.",
    model=os.getenv("MODEL_NAME_AGENT", "gemini-2.5-flash"),
    instruction=ROOT_AGENT_INSTRUCTION,
    tools=[analyze_document_next_chunk],
)

app = App(root_agent=root_agent, name="high_volume_document_analyzer")
