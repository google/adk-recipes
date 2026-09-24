import os

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...tools import triageQueryTool
from .prompt import agent_instructions

triage_agent = LlmAgent(
    model=os.getenv("MODEL_ID", "gemini-2.5-flash"),
    name="triage_agent",
    description="Assesses alert severity, deduplication, and context via SIEM",
    instruction=agent_instructions,
    tools=[FunctionTool(func=triageQueryTool)],
    output_key="triage_agent_output",
)
