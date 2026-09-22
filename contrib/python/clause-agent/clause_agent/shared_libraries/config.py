"""Central runtime configuration for ClauseIQ.

Every path/threshold is read from an environment variable *at call time*
(never cached at import time) so that tests and the mock Legal-review CLI
can point at isolated files without needing to reload modules.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent  # clause_agent/
PROJECT_ROOT = PACKAGE_DIR.parent


def get_contracts_dir() -> Path:
    """Directory containing the POC contract corpus (corpus.json)."""
    return Path(
        os.environ.get(
            "CLAUSE_AGENT_CONTRACTS_DIR",
            str(PACKAGE_DIR / "data" / "contracts"),
        )
    )


def get_memory_bank_path() -> Path:
    """Local JSON file backing `LocalJsonMemoryBankBackend`."""
    return Path(
        os.environ.get(
            "CLAUSE_AGENT_MEMORY_PATH",
            str(PROJECT_ROOT / "clause_agent_memory.json"),
        )
    )


def get_audit_log_path() -> Path:
    """Append-only JSONL log of corrections and Legal-review approvals."""
    return Path(
        os.environ.get(
            "CLAUSE_AGENT_AUDIT_LOG_PATH",
            str(PROJECT_ROOT / "clause_agent_audit_log.jsonl"),
        )
    )


def get_legal_queue_path() -> Path:
    """Local JSON file tracking pending/resolved Legal-review tasks."""
    return Path(
        os.environ.get(
            "CLAUSE_AGENT_LEGAL_QUEUE_PATH",
            str(PROJECT_ROOT / "clause_agent_legal_queue.json"),
        )
    )


def get_confidence_threshold() -> float:
    """Confidence bar below which hierarchy_resolver must escalate to Legal.

    PRD "Simulated Session" (Turn 1) cites 0.90 as the bar, with an example
    ruling at confidence 0.78 that must escalate rather than be answered
    directly.
    """
    return float(os.environ.get("CLAUSE_AGENT_CONFIDENCE_THRESHOLD", "0.90"))


def get_default_model() -> str:
    """Default Gemini model for all agents (root inherits to sub-agents)."""
    return (
        os.environ.get("MODEL_NAME")
        or os.environ.get("CLAUSE_AGENT_MODEL")
        or "gemini-3.5-flash"
    )
