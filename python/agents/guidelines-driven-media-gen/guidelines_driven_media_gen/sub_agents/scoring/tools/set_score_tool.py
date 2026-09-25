from google.adk.tools import ToolContext
import logging

logger = logging.getLogger(__name__)


def set_score(tool_context: ToolContext, total_score: int) -> str:
    logger.debug(f"Setting total score: {total_score}")
    tool_context.state["total_score"] = total_score
