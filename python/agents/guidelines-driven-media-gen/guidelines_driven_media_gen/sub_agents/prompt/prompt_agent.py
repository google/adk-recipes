from ... import config
from google.adk.agents import Agent
from .prompt import PROMPT
from ..tools.fetch_policy_tool import get_policy
from .tools.fetch_existing_assets import search_asset_bank

image_gen_prompt_generation_agent = Agent(
    name="image_gen_prompt_generation_agent",
    model=config.GENAI_MODEL,
    description=("You are an expert in creating image generation prompts for a particular brand"),
    instruction=(PROMPT),
    tools=[search_asset_bank, get_policy],
    output_key="image_gen_prompt",  # gets stored in session.state
)
