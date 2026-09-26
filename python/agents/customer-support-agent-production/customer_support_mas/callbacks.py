"""
Agent callbacks for the customer support system.

This module contains callback functions used by agents.
"""

import logging
import time

logger = logging.getLogger(__name__)

# Dictionary to track agent execution start times
_agent_execution_tracker = {}


def log_system_instructions(callback_context, llm_request):
    """
    Debug callback to log the full system instruction including preloaded memories.

    This runs BEFORE the model is called (before_model_callback).
    """
    try:
        agent_name = getattr(callback_context, "agent_name", "unknown")
        system_instruction = llm_request.config.system_instruction or "NO SYSTEM INSTRUCTION"

        has_memories = "<PAST_CONVERSATIONS>" in system_instruction
        memory_status = "MEMORIES FOUND" if has_memories else "NO MEMORIES INJECTED"

        logger.debug("System instruction for %s: %s", agent_name, memory_status)
        logger.debug("System instruction content for %s:\n%s", agent_name, system_instruction)

        if not has_memories:
            logger.debug("PreloadMemoryTool did not inject memories for %s", agent_name)

    except Exception as e:
        logger.error("Error logging system instruction: %s", e)


async def track_agent_start(callback_context):
    """
    Track when an agent starts execution.
    """
    try:
        session = callback_context._invocation_context.session
        agent_name = getattr(callback_context, "agent_name", "unknown")
        session_id = _extract_session_id(session)

        start_time = time.time()
        execution_key = f"{agent_name}:{session_id}"
        _agent_execution_tracker[execution_key] = start_time

        logger.debug("Agent '%s' starting (session: %s)", agent_name, session_id)

    except Exception as e:
        logger.error("Error tracking agent start: %s", e, exc_info=True)


async def auto_save_to_memory(callback_context):
    """
    Automatically save session to Memory Bank after each agent turn.

    Uses add_session_to_memory() which triggers async background consolidation.
    """
    callback_start_time = time.time()
    agent_name = "unknown"
    session_id = "unknown"

    try:
        memory_service = callback_context._invocation_context.memory_service
        session = callback_context._invocation_context.session

        agent_name = getattr(callback_context, "agent_name", "unknown")
        user_id = getattr(session, "user_id", "unknown") if session else "unknown"
        app_name = getattr(callback_context._invocation_context, "app_name", "NOT_SET")
        session_id = _extract_session_id(session)

        logger.debug(
            "Callback starting for agent: %s (session: %s, user: %s, app: %s)",
            agent_name,
            session_id,
            user_id,
            app_name,
        )

        if app_name == "NOT_SET" or app_name is None:
            logger.warning("app_name is NOT SET — Memory Bank requires app_name in scope")

        # Log agent execution time
        execution_key = f"{agent_name}:{session_id}"
        if execution_key in _agent_execution_tracker:
            total_execution_time = time.time() - _agent_execution_tracker.pop(execution_key)
            logger.debug("Agent %s execution time: %.2fs", agent_name, total_execution_time)
            if total_execution_time > 20:
                logger.warning("Slow agent: %s took %.2fs", agent_name, total_execution_time)
        else:
            logger.debug("No start time tracked for %s", agent_name)

        if not memory_service:
            logger.debug("Memory service not available")
            return

        memory_service_name = type(memory_service).__name__
        logger.debug("Memory service type: %s", memory_service_name)

        if memory_service_name == "InMemoryMemoryService":
            logger.debug("Using InMemoryMemoryService — memories will not persist across restarts")

        if not session:
            logger.debug("Session not available")
            return

        # Skip evaluation sessions
        if isinstance(session_id, str) and session_id.startswith("___eval___session___"):
            logger.debug("Skipping Memory Bank save for evaluation session")
            return

        # Save session to Memory Bank
        try:
            events = getattr(session, "events", [])
            logger.debug(
                "Saving session to Memory Bank (agent=%s, user=%s, events=%d)", agent_name, user_id, len(events)
            )

            if hasattr(memory_service, "add_session_to_memory"):
                result = await memory_service.add_session_to_memory(session)
                logger.debug("Session sent to Memory Bank, result: %s", result)
            elif hasattr(memory_service, "add_memory"):
                for event in events[-5:]:
                    await memory_service.add_memory(user_id=user_id, content=str(event), session_id=session_id)
                logger.debug("Events saved using add_memory fallback")
            else:
                logger.debug("Memory service has no add_session_to_memory method")

        except Exception as save_error:
            logger.error("Memory save failed: %s", save_error, exc_info=True)

    except Exception as e:
        logger.error("Callback error: %s", e)
    finally:
        duration = time.time() - callback_start_time
        logger.debug("Callback completed for %s in %.2fs", agent_name, duration)
        if duration > 5:
            logger.warning("Slow callback: %s took %.2fs", agent_name, duration)


async def check_hanging_agents():
    """
    Utility function to check for agents that started but haven't completed.
    """
    current_time = time.time()
    hanging_agents = []

    for execution_key, start_time in _agent_execution_tracker.items():
        elapsed = current_time - start_time
        if elapsed > 30:
            agent_name = execution_key.split(":")[0]
            hanging_agents.append({"agent": agent_name, "execution_key": execution_key, "elapsed_seconds": elapsed})
            logger.warning("Hanging agent detected: %s has been running for %.2fs", agent_name, elapsed)

    return hanging_agents


def _extract_session_id(session):
    """Extract session ID from a session object."""
    if not session:
        return "unknown"
    return (
        getattr(session, "session_id", None)
        or getattr(session, "id", None)
        or getattr(session, "name", None)
        or "unknown"
    )
