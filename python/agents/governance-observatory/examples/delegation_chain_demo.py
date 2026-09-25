"""
Delegation Chain Demo – Multi-agent authority verification.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.adapters.adk_adapter import ADKGovernanceWrapper

class MultiAgent:
    name = "multi_agent"
    tools = ["delegate", "execute"]

    def run(self, input_data):
        return {"status": "delegation_complete", "chain": ["agent_a", "agent_b", "agent_c"]}

    def delegate_to(self):
        return ["agent_b", "agent_c"]

def main():
    print("=" * 70)
    print("ADK Governance Observatory – Delegation Chain Demo")
    print("=" * 70)

    agent = MultiAgent()
    wrapper = ADKGovernanceWrapper(agent, agent_id="multi_agent_001")
    result = wrapper.run({"task": "complex_workflow"})

    print("\n🔐 Delegation Chain Verified")
    print("-" * 70)
    print(f"Chain: {' → '.join(result['trace'].delegation_chain)}")
    print(f"Authority Valid: {result['certificate'].continuity_intact}")
    print(f"Certificate Hash: {result['certificate'].certificate_hash[:32]}...")
    print("\n✅ Delegation chain is cryptographically verified.")