import logging

from google.adk.tools import ToolContext

logger = logging.getLogger(__name__)


async def get_image(tool_context: ToolContext):
    try:
        logger.debug("Entered the get_image function")
        artifact_name = (
            "generated_image_"
            + str(tool_context.state.get("loop_iteration", 0))
            + ".png"
        )
        logger.debug(f"artifact_name: {artifact_name}")
        await tool_context.load_artifact(artifact_name)
        logger.debug("artifact loaded successfully")

        return {
            "status": "success",
            "message": f"Image artifact {artifact_name} successfully loaded.",
        }
    except Exception as e:
        logger.error(f"Error loading artifact {artifact_name}: {e!s}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error loading artifact {artifact_name}: {e!s}",
        }
