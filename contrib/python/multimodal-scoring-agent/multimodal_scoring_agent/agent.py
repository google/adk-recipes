import json
import logging
import os

from google.adk.agents import Agent
from pydantic import BaseModel, Field, ValidationError


# Define the Structured Output Schema
class ScoringResult(BaseModel):
    grade: str = Field(
        description="The deterministic grade assigned based on the rubric."
    )
    reasoning: str = Field(
        description="Detailed reasoning for why the grade was given, referencing specific criteria from the rubric."
    )
    confidence: float = Field(
        description="Confidence score in the assessment, from 0.0 to 1.0."
    )


# Initialize the Agent with structured output
scoring_agent = Agent(
    name="multimodal_scoring_agent",
    model=os.getenv("MODEL_NAME"),  # Strong multimodal capabilities
    description="Evaluates images against a strict scoring rubric and outputs deterministic grades.",
    instruction="""You are an expert visual quality assurance inspector. 
You will be provided with an image and a JSON-defined scoring rubric. 
Your task is to carefully analyze the image against the criteria specified in the rubric.
Apply the rules strictly and objectively. 
Return your final evaluation using the required structured output format, including the final grade, detailed reasoning referencing the rubric, and your confidence level.""",
    output_schema=ScoringResult,
)


def evaluate_image(image_path: str, rubric_path: str):
    """
    Evaluates an image against a JSON rubric using the visual scoring agent.
    """
    import asyncio
    import uuid

    from google.adk.runners import InMemoryRunner
    from google.genai import types

    # Load the rubric
    with open(rubric_path) as f:
        rubric_data = json.load(f)

    rubric_str = json.dumps(rubric_data, indent=2)
    prompt = f"Please evaluate the attached image using the following scoring rubric:\n\n{rubric_str}"

    print(f"Evaluating '{image_path}' using rubric '{rubric_path}'...")

    # Read the image bytes directly
    with open(image_path, "rb") as f:
        img_data = f.read()

    # Format as a GenAI types.Content object
    content = types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=img_data, mime_type="image/jpeg"),
            types.Part.from_text(text=prompt),
        ],
    )

    async def run_agent():
        runner = InMemoryRunner(agent=scoring_agent)
        # Tell the runner to implicitly create the session in its memory service
        runner.auto_create_session = True

        events = runner.run_async(
            user_id="local_user",
            session_id=str(uuid.uuid4()),
            new_message=content,
        )
        # The agent streams events back, containing the JSON text
        result = None
        async for event in events:
            if (
                hasattr(event, "message")
                and event.message
                and event.message.parts
            ):
                part = event.message.parts[0]
                if hasattr(part, "text") and part.text is not None:
                    try:
                        result = ScoringResult.model_validate_json(part.text)
                    except ValidationError as e:
                        logging.error(f"Validation failure: {e}")
                    except Exception as e:
                        logging.error(f"Unexpected error parsing response: {e}")
        return result

    return asyncio.run(run_agent())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Visual Scoring Agent")
    parser.add_argument(
        "--image", required=True, help="Path to the image to score"
    )
    parser.add_argument(
        "--rubric", required=True, help="Path to the JSON rubric"
    )

    args = parser.parse_args()
    try:
        result = evaluate_image(args.image, args.rubric)
        print("\n=== Scoring Result ===")
        if result is None:
            print("Evaluation failed. Result is None.")
        else:
            print(f"Grade: {result.grade}")
            print(f"Confidence: {result.confidence}")
            print(f"Reasoning:\n{result.reasoning}")
    except Exception as e:
        print(f"Error during evaluation: {e}")
