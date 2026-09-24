import logging
import os

from google.adk.agents import LlmAgent
from google.adk.planners import BuiltInPlanner
from google.genai import types

from .prompt import root_agent_instruction  # Instruction for the orch_agent LLM persona
from .sub_agents.investigation.agent import investigation_agent
from .sub_agents.response.agent import response_agent
from .sub_agents.threatintel.agent import threatintel_agent
from .sub_agents.triage.agent import triage_agent

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

root_agent = LlmAgent(
    model=os.getenv("MODEL_ID", "gemini-2.5-flash"),
    name="cyber_guardian_orchestrator",
    description="Orchestrates a multi-agent cybersecurity incident response workflow",
    instruction=root_agent_instruction,
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_budget=512)
    ),
    sub_agents=[threatintel_agent, investigation_agent, triage_agent, response_agent],
)

from google.adk.apps import App  # ruff: noqa: E402

app = App(root_agent=root_agent, name="cyber_guardian")
