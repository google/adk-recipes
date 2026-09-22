"""Hierarchy Resolver sub-agent.

Resolves which document/clause is legally controlling when a customer's
contracts conflict, escalating precedent-setting or low-confidence rulings
to a human Legal Reviewer instead of guessing (PRD §3, §8).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.long_running_tool import LongRunningFunctionTool

from clause_agent.prompt import (
    get_hierarchy_resolver_instruction,
)
from clause_agent.shared_libraries import config
from clause_agent.tools.document_search import search_documents
from clause_agent.tools.legal_review import request_legal_review
from clause_agent.tools.memory_bank import (
    memory_bank_create,
    memory_bank_search,
)


def build_hierarchy_resolver(model: str | BaseLlm | None = None) -> LlmAgent:
    """Builds the Hierarchy Resolver sub-agent.

    Args:
      model: Override the model (e.g. a fake `BaseLlm` for tests). Defaults
        to `config.get_default_model()`.
    """
    return LlmAgent(
        name="hierarchy_resolver",
        model=model or config.get_default_model(),
        description=(
            "Resolves which contract/clause is legally controlling when a"
            " customer has multiple, conflicting documents (base contract,"
            " amendments, renewals). Escalates to Legal for"
            " precedent-setting or low-confidence rulings instead of"
            " guessing. Does NOT extract specific field values on its own"
            " -- that is clause_extractor."
        ),
        instruction=get_hierarchy_resolver_instruction(),
        tools=[
            memory_bank_search,
            search_documents,
            LongRunningFunctionTool(func=request_legal_review),
            memory_bank_create,
        ],
    )
