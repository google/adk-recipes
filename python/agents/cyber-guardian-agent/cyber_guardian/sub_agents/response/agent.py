import os

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from ...tools import getPlaybookTool, responseExecutionTool
from .prompt import agent_instructions

response_agent = LlmAgent(
    model=os.getenv("MODEL_ID", "gemini-2.5-flash"),
    name="response_agent",
    description="Recommends and triggers incident response actions",
    instruction=agent_instructions,
    tools=[
        FunctionTool(func=responseExecutionTool),
        FunctionTool(func=getPlaybookTool),
    ],
    output_key="response_agent_output",
)
