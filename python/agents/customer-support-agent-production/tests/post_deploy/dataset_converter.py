"""Convert between ADK EvalSet format and Vertex AI Evaluation Service format.

ADK evalsets (.evalset.json) use a nested structure with conversations,
invocation events, function calls, etc. The Vertex AI Eval Service expects
a pandas DataFrame with prompt, reference, and expected_tool_use columns.

Usage:
    from tests.post_deploy.dataset_converter import adk_evalset_to_dataframe

    df = adk_evalset_to_dataframe("tests/integration/product_agent_handoffs.evalset.json")
    print(df)
"""

import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def adk_evalset_to_dataframe(evalset_path: str) -> pd.DataFrame:
    """Convert an ADK EvalSet (.evalset.json) to a Vertex AI eval DataFrame.

    Extracts the first turn from each eval case to create single-turn
    evaluation prompts with reference responses and expected tool use.

    Args:
        evalset_path: Path to the ADK evalset JSON file.

    Returns:
        DataFrame with columns: prompt, reference, expected_tool_use
    """
    path = Path(evalset_path)
    if not path.exists():
        raise FileNotFoundError(f"Evalset not found: {evalset_path}")

    with open(path) as f:
        evalset = json.load(f)

    rows = []
    for case in evalset.get("eval_cases", []):
        eval_id = case.get("eval_id", "unknown")
        conversation = case.get("conversation", [])

        for turn in conversation:
            # Extract user prompt
            user_content = turn.get("user_content", {})
            parts = user_content.get("parts", [])
            prompt = parts[0].get("text", "") if parts else ""

            # Extract reference response
            final_response = turn.get("final_response", {})
            resp_parts = final_response.get("parts", [])
            reference = resp_parts[0].get("text", "") if resp_parts else ""

            # Extract expected tool calls from intermediate_data
            tool_calls = _extract_tool_calls(turn.get("intermediate_data", {}))

            rows.append(
                {
                    "eval_id": eval_id,
                    "prompt": prompt,
                    "reference": reference,
                    "expected_tool_use": json.dumps(tool_calls),
                }
            )

    df = pd.DataFrame(rows)
    logger.info(
        "[CONVERTER] Converted %s: %d cases, %d total turns",
        path.name,
        len(evalset.get("eval_cases", [])),
        len(rows),
    )
    return df


def _extract_tool_calls(intermediate_data: dict) -> list[dict]:
    """Extract tool call name/args from ADK invocation events."""
    tool_calls = []
    for event in intermediate_data.get("invocation_events", []):
        content = event.get("content", {})
        for part in content.get("parts", []):
            fc = part.get("function_call")
            if fc:
                tool_calls.append(
                    {
                        "tool_name": fc.get("name", ""),
                        "tool_input": fc.get("args", {}),
                    }
                )
    return tool_calls


def vertex_results_to_json(eval_result) -> dict:
    """Convert a Vertex AI EvalResult to a JSON-serializable dict.

    Args:
        eval_result: The result object from client.evals.create_evaluation_run()
                     or EvalTask.evaluate().

    Returns:
        dict with summary_metrics and per-row metrics.
    """
    result = {
        "summary_metrics": {},
        "rows": [],
    }

    # Handle client.evals result (has .name, .evaluation_items)
    if hasattr(eval_result, "evaluation_items"):
        result["name"] = getattr(eval_result, "name", None)
        for item in eval_result.evaluation_items or []:
            row = {}
            if hasattr(item, "metrics"):
                for metric_name, metric_val in (item.metrics or {}).items():
                    row[metric_name] = getattr(metric_val, "score", None)
            result["rows"].append(row)

    # Handle EvalTask result (has .summary_metrics, .metrics_table)
    if hasattr(eval_result, "summary_metrics"):
        result["summary_metrics"] = dict(eval_result.summary_metrics or {})
    if hasattr(eval_result, "metrics_table"):
        try:
            result["rows"] = eval_result.metrics_table.to_dict(orient="records")
        except Exception:
            pass

    return result
