import os

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...tools import threatIntelQueryTool
from .prompt import agent_instructions

threatintel_agent = LlmAgent(
    model=os.getenv("MODEL_ID", "gemini-2.5-flash"),
    name="threat_intel_agent",
    description="Enriches IPs/domains/hashes with threat intelligence context",
    instruction=agent_instructions,
    tools=[FunctionTool(func=threatIntelQueryTool)],
    output_key="threatintel_agent_output",
)
