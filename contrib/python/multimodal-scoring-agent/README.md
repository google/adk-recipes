# Multimodal Scoring Agent

This sample demonstrates how to build a visual quality assurance agent using the Google ADK. The agent takes an image and a strictly defined JSON scoring rubric, and uses Gemini's multimodal and structured output capabilities to return a deterministic grade and reasoning.

## Use Case
Enterprises often struggle to automate subjective visual tasks, such as grading manufacturing defects, evaluating document layouts, or classifying damage on return items. Traditional computer vision requires large datasets and custom training for each defect type. By leveraging Gemini 3.5 Flash and Google ADK, you can define your quality standards in simple JSON and let the agent apply those rules strictly and objectively.

## Structure
- `agent.py`: Contains the `Agent` definition and execution logic. It enforces a Pydantic `ScoringResult` schema to ensure the model always returns a `grade`, `reasoning`, and `confidence`.
- `rubric.json`: A sample scoring rubric for circuit board quality assurance. You can replace this with your own domain-specific rules (e.g., the Mensa scale, document verification rules).

## Requirements
```bash
uv sync
```

## Usage
Run the agent by providing it an image to evaluate and the JSON rubric:

```bash
uv run python -m multimodal_scoring_agent.agent --image images/pass_board.jpg --rubric rubric.json
```
## Example Output
```
=== Scoring Result ===
Grade: Fail - Critical
Confidence: 0.95
Reasoning:
Based on the Circuit Board Quality Assurance rubric, the image contains visible scorch marks near the main processor chip. This meets the criteria for a "Fail - Critical" grade as outlined in the conditions.
```
