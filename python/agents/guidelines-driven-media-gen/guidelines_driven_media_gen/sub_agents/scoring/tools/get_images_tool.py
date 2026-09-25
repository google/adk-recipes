from google.adk.tools import ToolContext
import logging

logger = logging.getLogger(__name__)


async def get_image(tool_context: ToolContext):
    try:
        logger.debug("Entered the get_image function")
        artifact_name = (
            f"generated_image_" + str(tool_context.state.get("loop_iteration", 0)) + ".png"
        )
        logger.debug(f"artifact_name: {artifact_name}")
        artifact = await tool_context.load_artifact(artifact_name)
        logger.debug(f"artifact loaded successfully")


        return {
            "status": "success",
            "message": f"Image artifact {artifact_name} successfully loaded."
        }
    except Exception as e:
        logger.error(f"Error loading artifact {artifact_name}: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error loading artifact {artifact_name}: {str(e)}"
        }
