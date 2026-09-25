from google.adk.agents import Agent

from ... import config
from .prompt import IMAGE_GEN_PROMPT
from .tools.image_generation_tool import generate_images

image_generation_agent = Agent(
    name="image_generation_agent",
    model=config.GENAI_MODEL,
    description=("You are an expert in creating images with {config.IMAGE_GEN_MODEL}"),
    instruction=(IMAGE_GEN_PROMPT),
    tools=[generate_images],
    output_key="output_image",
)
