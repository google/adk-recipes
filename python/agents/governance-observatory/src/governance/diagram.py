"""
Governance Diagram – Generates ASCII architecture diagrams for PRs and LinkedIn.
"""

def generate_governance_diagram() -> str:
    """Generate the Governance Observatory architecture diagram."""
    return """
┌─────────────────────────────────────────────────────────────────────────────┐
│                      ADK Governance Observatory                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                │
│   │   ADK Agent  │───▶│  Execution   │───▶│  Capability  │                │
│   │              │    │    Trace     │    │   Witness    │                │
│   └──────────────┘    └──────────────┘    └──────────────┘                │
│                                                      │                      │
│                                                      ▼                      │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                │
│   │  Governance  │◀───│  Counter-    │◀───│  Replay      │                │
│   │  Certificate │    │  factual     │    │  Engine      │                │
│   │              │    │  Verification│    │              │                │
│   └──────────────┘    └──────────────┘    └──────────────┘                │
│                                                                             │
│   OUTPUT:                                                                   │
│   • Capability Witness (scientific, counterfactual proof)                   │
│   • Delegation Chain (cryptographic authority verification)                 │
│   • Constitutional Execution Certificate (replayable, verifiable)          │
│   • Governance Verdict (PASS / WARNING / CRITICAL / DENIED)                │
│   • Governance Score (0-100)                                               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
"""