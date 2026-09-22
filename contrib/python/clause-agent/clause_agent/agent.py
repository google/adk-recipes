"""ClauseIQ root agent: Contract Hierarchy & Billing Terms Orchestrator.

Wires the two feedback loops from the PRD ("How it works -- two feedback
loops"):
  - Legal loop (upstream): hierarchy_resolver <-> Legal Reviewer, via the
    blocking `request_legal_review` LongRunningFunctionTool.
  - Business loop (downstream): clause_extractor <-> Billing/AR Analyst,
    via the non-blocking `submit_correction` tool.

Both loops write into the same scoped Memory Bank (see
`clause_agent/tools/memory_bank.py`), which both sub-agents check before
acting.

Usage:
    adk web clause_agent
    adk run clause_agent
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm

from clause_agent.prompt import ROOT_AGENT_INSTRUCTION
from clause_agent.shared_libraries import config
from clause_agent.sub_agents.clause_extractor import build_clause_extractor
from clause_agent.sub_agents.hierarchy_resolver import build_hierarchy_resolver
from clause_agent.tools.sap_connector import check_sap_invoice_status


def build_root_agent(model: str | BaseLlm | None = None) -> LlmAgent:
    """Builds the full ClauseIQ agent tree (root + both sub-agents).

    Args:
      model: Override the model for ALL agents in the tree (e.g. a fake
        `BaseLlm` for tests). Defaults to `config.get_default_model()`.
    """
    return LlmAgent(
        name="root_agent",
        model=model or config.get_default_model(),
        description=(
            "ClauseIQ Orchestrator: routes contract precedence and"
            " billing-term extraction questions to the right specialist,"
            " and enforces that every answer is cited and every ruling is"
            " either previously approved or freshly Legal-approved."
        ),
        instruction=ROOT_AGENT_INSTRUCTION,
        tools=[check_sap_invoice_status],
        sub_agents=[
            build_hierarchy_resolver(model=model),
            build_clause_extractor(model=model),
        ],
    )


root_agent = build_root_agent()
