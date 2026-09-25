from google.adk.agents import Agent
from ... import config
from ..tools.fetch_policy_tool import get_policy
from .tools.get_images_tool import get_image
from .tools.set_score_tool import set_score
from .prompt import SCORING_PROMPT


scoring_agent = Agent(
    name="scoring_agent",
    model=config.GENAI_MODEL,
    description=(
        "You are an expert in evaluating and scoring images based on various criteria "
        "provided to you."
    ),
    instruction=(SCORING_PROMPT),
    output_key="scoring",
    tools=[get_policy, get_image, set_score],
)
