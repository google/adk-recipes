"""Clause Extraction sub-agent.

Extracts a specific billing field's value (payment term, customer ID,
billing entity, etc.) with a citation, and handles corrections from
downstream Billing/AR users, generalizing lessons via Memory Bank rules
(PRD §3, TC3).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm

from clause_agent.prompt import CLAUSE_EXTRACTOR_INSTRUCTION
from clause_agent.shared_libraries import config
from clause_agent.tools.correction import submit_correction
from clause_agent.tools.document_search import search_documents
from clause_agent.tools.memory_bank import (
    memory_bank_create,
    memory_bank_search,
)


def build_clause_extractor(model: str | BaseLlm | None = None) -> LlmAgent:
    """Builds the Clause Extraction sub-agent.

    Args:
      model: Override the model (e.g. a fake `BaseLlm` for tests). Defaults
        to `config.get_default_model()`.
    """
    return LlmAgent(
        name="clause_extractor",
        model=model or config.get_default_model(),
        description=(
            "Extracts a specific billing-relevant field's value (payment"
            " term, customer ID, billing entity, currency, etc.) from a"
            " customer's contracts, with a citation. Accepts and records"
            " corrections from downstream Billing/AR users. Does NOT"
            " resolve precedence conflicts between contracts -- that is"
            " hierarchy_resolver."
        ),
        instruction=CLAUSE_EXTRACTOR_INSTRUCTION,
        tools=[
            memory_bank_search,
            search_documents,
            submit_correction,
            memory_bank_create,
        ],
    )
