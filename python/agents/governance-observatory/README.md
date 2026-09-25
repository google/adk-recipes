# ADK Governance Observatory

**Discover capabilities before they become incidents.**

## What This Is

The ADK Governance Observatory adds a **runtime governance layer** to Google's Agent Development Kit (ADK). It answers a question that ADK currently does not:

> "Why was this agent allowed to execute this action at this exact moment?"

## The Problem

ADK records execution. It does not **prove execution authority**.

- Policies change between approval and execution.
- Delegation chains drift without re-authorisation.
- Evidence expires while the agent continues acting.
- Capabilities emerge across agents that no single agent was authorised to perform.

## What This Solves

| Problem | Solution |
|---------|----------|
| No runtime governance | Constitutional Execution Certificates |
| No delegation accountability | Cryptographic delegation chain verification |
| No capability drift detection | Capability Witness + DCR integration |

## Architecture
Google ADK Agent
↓
Execution Trace
↓
Capability Witness
↓
Counterfactual Verification
↓
Constitutional Execution Certificate
↓
Replay Engine


## Quick Start

```bash
git clone https://github.com/a1k7/adk-governance-observatory
cd adk-governance-observatory
pip install -r requirements.txt
python examples/customer_service_demo.py

Integration with ADK

python
from src.adapters.adk_adapter import ADKGovernanceWrapper

agent = YourADKAgent()
governed_agent = ADKGovernanceWrapper(agent)
result = governed_agent.run()
# result includes: trace, certificate, verification_status


Output Example

json
{
  "execution_id": "exec-2026-06-21-001",
  "continuity_valid": true,
  "evidence_freshness": true,
  "admissibility": "ADMISSIBLE",
  "delegation_chain": ["agent_a", "agent_b", "tool_c"],
  "certificate_hash": "sha256:7f1a8c3e...",
  "replayable": true
}
License

Apache 2.0 – same as Google ADK.

# ADK Governance Observatory

**OpenTelemetry tells you what happened.**
**Governance Observatory tells you whether the agent should have been allowed to do it.**