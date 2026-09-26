"""
Main entry point for the customer support multi-agent system.

This module provides the primary interface for importing and using the agent system.

USAGE:
    # Local testing
    from customer_support_mas.main import root_agent
    from vertexai import agent_engines

    app = agent_engines.AdkApp(agent=root_agent)
    session = await app.async_create_session(user_id="user123")
    response = await app.async_query(
        user_id="user123",
        session_id=session.id,
        message="Show me laptops under $600"
    )

    # Production deployment
    remote_app = agent_engines.create(
        agent_engine=app,
        requirements=[...],
        extra_packages=["customer_support_mas"],
        display_name="customer-support-multiagent"
    )
"""

import logging
import os

logger = logging.getLogger(__name__)


def configure() -> None:
    """Load environment and configure logging. Call once at application startup."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "WARNING")),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise ValueError("GOOGLE_CLOUD_PROJECT environment variable must be set")

    logger.debug("Customer Support Multi-Agent System configured (project: %s)", project_id)


from customer_support_mas.agents.root import root_agent  # noqa: E402

__all__ = ["root_agent", "configure"]
