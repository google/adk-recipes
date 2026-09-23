# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.artifacts import (
    GcsArtifactService,
    InMemoryArtifactService,
)
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import (
    InMemorySessionService,
    VertexAiSessionService,
)

from presentation_agent.prompt import final_instruction

# Local Application Imports from the 'presentation_agent' package
# If need to include MODEL_ARMOR_TEMPLATE_ID and related imports, they would go here as well.
from presentation_agent.shared_libraries.config import (
    ENABLE_DEEP_RESEARCH,
    ENABLE_RAG,
    GCS_BUCKET_NAME,
    ROOT_MODEL,
    get_gcs_client,
    get_logger,
    initialize_genai_client,
    resolve_regional_location,
)
from presentation_agent.sub_agents import (
    batch_slide_writer_tool,
    deep_research_agent_tool,
    generate_outline_and_save_tool,
    google_research_tool,
    internal_knowledge_search_tool,
    outline_specialist_tool,
    slide_writer_specialist_tool,
)
from presentation_agent.tools import ALL_STANDARD_TOOLS


class PresentationExpertApp:
    """
    Encapsulates the agent and runner for the Presentation Expert.
    Initializes all clients, services, and the main orchestrating agent.
    """

    def __init__(self):
        initialize_genai_client()

        # 1. Start with the core tools that are always needed
        agent_tools = [
            *ALL_STANDARD_TOOLS,
            # Specialist / Research Tools
            outline_specialist_tool,
            generate_outline_and_save_tool,
            slide_writer_specialist_tool,
            batch_slide_writer_tool,
            google_research_tool,
        ]

        # 2. Conditionally add RAG (Internal Search)
        if ENABLE_RAG:
            agent_tools.append(internal_knowledge_search_tool)

        # 3. Conditionally add Deep Research (Slow, analytical)
        if ENABLE_DEEP_RESEARCH:
            agent_tools.append(deep_research_agent_tool)

        # 4. Configure Enterprise Guardrails (Model Armor)
        agent_kwargs = {
            "model": ROOT_MODEL,
            "name": "presentation_expert_agent",
            "description": (
                "A master AI assistant for creating and editing professional "
                "PowerPoint presentations."
            ),
            "instruction": final_instruction,
            "tools": agent_tools,
        }

        # if MODEL_ARMOR_TEMPLATE_ID:
        #    get_logger("agent").info(
        #         f"Applying Model Armor Template: {MODEL_ARMOR_TEMPLATE_ID}"
        #     )
        #    agent_kwargs["before_agent_callback"] = model_armor_interceptor
        #    agent_kwargs["after_model_callback"] = model_armor_response_interceptor

        # Instantiate the Main Agent
        self._agent = Agent(**agent_kwargs)

        # Configure Artifact Service (GCS or In-Memory)
        artifact_service = None
        if GCS_BUCKET_NAME:
            try:
                gcs_client = get_gcs_client()
                if gcs_client:
                    gcs_client.get_bucket(GCS_BUCKET_NAME)
                    artifact_service = GcsArtifactService(
                        bucket_name=GCS_BUCKET_NAME
                    )
                    get_logger("agent").info(
                        f"Successfully connected to GCS ArtifactService. Bucket: {GCS_BUCKET_NAME}"
                    )
                else:
                    raise RuntimeError("GCS client could not be initialized.")
            except Exception as e:
                get_logger("agent").warning(
                    f"Failed to initialize GcsArtifactService: {e}"
                )
                get_logger("agent").warning(
                    "Falling back to InMemoryArtifactService."
                )
                artifact_service = InMemoryArtifactService()
        else:
            get_logger("agent").info(
                "GCS_BUCKET_NAME not set. Using InMemoryArtifactService."
            )
            artifact_service = InMemoryArtifactService()

        # Configure Session Service (Persistent Vertex AI or Local In-Memory)
        is_local = (os.getenv("LOCAL_DEV") or "").lower() == "true"
        session_location = resolve_regional_location(
            os.getenv("GOOGLE_CLOUD_LOCATION")
        )
        if not is_local and os.getenv("GOOGLE_CLOUD_PROJECT"):
            session_service = VertexAiSessionService(
                project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                location=session_location,
            )
            get_logger("agent").info(
                f"Using VertexAiSessionService (Project: {os.getenv('GOOGLE_CLOUD_PROJECT')})"
            )
        else:
            session_service = InMemorySessionService()
            get_logger("agent").info(
                "Using InMemorySessionService for local development."
            )

        # Configure and Run the Runner
        self._runner = Runner(
            agent=self._agent,
            app_name="presentation_agent",
            session_service=session_service,
            artifact_service=artifact_service,
            memory_service=InMemoryMemoryService(),
        )
        get_logger("agent").info("PresentationExpertApp initialized.")


# This global instance is what the ADK CLI will look for and run.
coordinator_wrapper = PresentationExpertApp()
root_agent = coordinator_wrapper._agent

app = App(root_agent=root_agent, name="presentation_agent")
