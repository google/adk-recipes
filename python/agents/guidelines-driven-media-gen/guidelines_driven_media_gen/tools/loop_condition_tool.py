from google.adk.tools import ToolContext, FunctionTool
from .. import config
import logging

logger = logging.getLogger(__name__)

def check_condition_and_escalate_tool(tool_context: ToolContext) -> dict:
    """Checks the loop termination condition and escalates if met or max count reached."""
 

    # Increment loop iteration count using state
    current_loop_count = tool_context.state.get("loop_iteration", 0)
    current_loop_count += 1
    tool_context.state["loop_iteration"] = current_loop_count

    # Define maximum iterations
    max_iterations = config.MAX_ITERATIONS

    # Get the condition result set by the sequential agent from state
    total_score = tool_context.state.get("total_score", 50)

    condition_met = total_score > config.SCORE_THRESHOLD

    response_message = f"Check iteration {current_loop_count}: Sequential condition met = {condition_met}. "

    # Check if the condition is met OR maximum iterations are reached
    if condition_met:
        logger.info("Condition met. Setting escalate=True to stop the LoopAgent.")
        tool_context.actions.escalate = True
        response_message += "Condition met, stopping loop."
    elif current_loop_count >= max_iterations:
        logger.info(
            f"Max iterations ({max_iterations}) reached. Setting escalate=True to stop the LoopAgent."
        )
        tool_context.actions.escalate = True
        response_message += "Max iterations reached, stopping loop."
    else:
        logger.info(
            "Condition not met and max iterations not reached. Loop will continue."
        )
        response_message += "Loop continues."

    return {"status": "Evaluated scoring condition", "message": response_message}




check_tool_condition = FunctionTool(func=check_condition_and_escalate_tool)
