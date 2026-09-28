# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Memory Bank topic + structured-profile configuration.

Defines the Memory Bank topics plus a native Structured Profile schema (the
dream-review user profile). The same ``memory_bank_config`` object is applied to the Agent Engine
by ``scripts/provision_agent_engine.py`` and is the single source of truth for
the profile schema id + scope used by the generate/retrieve paths in
``app/memory/dream_review.py`` and ``app/memory/user_profile.py``.
"""

from google.genai.types import Schema, Type
from vertexai._genai.types import (
    ManagedTopicEnum,
    MemoryType,
    StructuredMemoryConfig,
    StructuredMemorySchemaConfig,
)
from vertexai._genai.types import (
    MemoryBankCustomizationConfig as CustomizationConfig,
)
from vertexai._genai.types import (
    MemoryBankCustomizationConfigMemoryTopic as MemoryTopic,
)
from vertexai._genai.types import (
    MemoryBankCustomizationConfigMemoryTopicManagedMemoryTopic as ManagedMemoryTopic,
)
from vertexai._genai.types import (
    ReasoningEngineContextSpecMemoryBankConfig as MemoryBankConfig,
)

# Schema id doubles as the key in retrieve_profiles' response dict. Must be
# 1-63 chars, lowercase-leading, [a-z0-9-].
USER_PROFILE_SCHEMA_ID = "user-profile"

_USER_PROFILE_SCHEMA = Schema(
    type=Type.OBJECT,
    properties={
        "summary": Schema(
            type=Type.STRING,
            description=(
                "A cohesive narrative of who the user is, what they care "
                "about, and how they work — not a list of facts."
            ),
        ),
        "role": Schema(
            type=Type.STRING,
            description="The user's role, occupation, or primary domain.",
        ),
        "interests": Schema(
            type=Type.ARRAY,
            items=Schema(type=Type.STRING),
            description="Durable topics, tools, or areas the user cares about.",
        ),
        "working_style": Schema(
            type=Type.STRING,
            description="How the user prefers to work and be communicated with.",
        ),
        "durable_facts": Schema(
            type=Type.ARRAY,
            items=Schema(type=Type.STRING),
            description="Stable, non-ephemeral facts worth remembering.",
        ),
    },
)


def profile_scope(app_name: str, user_id: str) -> dict[str, str]:
    """Scope dict shared by profile generation and retrieval.

    Generate and retrieve MUST use the identical scope or retrieval silently
    returns nothing, so both paths go through this helper.
    """
    return {"app_name": app_name, "user_id": user_id}


memory_bank_config = MemoryBankConfig(
    customization_configs=[
        CustomizationConfig(
            memory_topics=[
                MemoryTopic(
                    managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.USER_PERSONAL_INFO,
                    ),
                ),
                MemoryTopic(
                    managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.USER_PREFERENCES,
                    ),
                ),
                MemoryTopic(
                    managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.EXPLICIT_INSTRUCTIONS,
                    ),
                ),
                MemoryTopic(
                    managed_memory_topic=ManagedMemoryTopic(
                        managed_topic_enum=ManagedTopicEnum.KEY_CONVERSATION_DETAILS,
                    ),
                ),
            ],
        ),
    ],
    structured_memory_configs=[
        StructuredMemoryConfig(
            # Matches the scope dict used by generate/retrieve (profile_scope):
            # one profile maintained per (app_name, user_id).
            scope_keys=["app_name", "user_id"],
            schema_configs=[
                StructuredMemorySchemaConfig(
                    id=USER_PROFILE_SCHEMA_ID,
                    memory_type=MemoryType.STRUCTURED_PROFILE,
                    memory_schema=_USER_PROFILE_SCHEMA,
                ),
            ],
        ),
    ],
)
