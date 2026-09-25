"""
Customer Service Agent – Governance Observatory Demo with Enhanced Output
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.adapters.adk_adapter import ADKGovernanceWrapper

class CustomerServiceAgent:
    name = "customer_service"
    tools = ["read_ticket", "update_ticket", "send_email"]

    def run(self, input_data):
        # Simulate a successful execution
        return {
            "status": "success",
            "ticket": input_data.get("ticket_id", "INC-0001"),
            "action": "escalated",
            "agent": "customer_service"
        }

    def delegate_to(self):
        # Return a list of agents to delegate to
        return ["triage_agent", "specialist_agent"]

def main():
    print("=" * 70)
    print("ADK Governance Observatory – Customer Service Demo")
    print("=" * 70)

    agent = CustomerServiceAgent()
    wrapper = ADKGovernanceWrapper(agent, agent_id="customer_service_001")
    result = wrapper.run({"ticket_id": "INC-12345", "issue": "payment failure"})

    print("\n📊 Execution Result")
    print("-" * 70)
    print(f"Agent ID: {result['trace'].agent_id}")
    print(f"Final Decision: {result['trace'].final_decision.value}")
    print(f"Verification Status: {result['verification_status']}")

    print("\n🔐 Delegation Chain")
    print("-" * 70)
    if result['trace'].delegation_chain:
        for i, agent in enumerate(result['trace'].delegation_chain):
            print(f"  {i+1}. {agent}")
    else:
        print("  No delegation recorded")

    print("\n🔧 Tools Called")
    print("-" * 70)
    if result['trace'].tools_called:
        for tool in result['trace'].tools_called:
            print(f"  - {tool}")
    else:
        print("  No tools recorded")

    print("\n📜 Capability Witnesses")
    print("-" * 70)
    if result['witnesses']:
        for witness in result['witnesses']:
            print(f"\n  🔍 {witness.name}")
            print(f"     Severity: {witness.severity}")
            print(f"     Confidence: {witness.confidence:.0%}")
            print(f"     Recommendation: {witness.governance_recommendation}")
            print(f"     Evidence: {len(witness.evidence_events)} events")
            print("     Counterfactual:")
            for cf in witness.counterfactual_results:
                status = "disappears" if not cf.capability_still_exists else "still exists"
                print(f"       - Remove '{cf.removed_action}' → capability {status}")
            print(f"     Affected Agents: {', '.join(witness.affected_agents)}")
    else:
        print("  No capability witnesses detected.")
        print("  This could mean:")
        print("    - All actions were within expected patterns")
        print("    - The agent execution completed successfully")
        print("    - No emergent or unknown capabilities were discovered")

    print("\n📜 Constitutional Execution Certificate")
    print("-" * 70)
    cert = result['certificate']
    print(f"  Execution ID: {cert.execution_id}")
    print(f"  Verified: {cert.verified}")
    print(f"  Final State: {cert.final_state.value}")
    if cert.denial_reason:
        print(f"  Denial Reason: {cert.denial_reason.reason.value}")
        print(f"    {cert.denial_reason.description}")
        if cert.denial_reason.threshold:
            print(f"    Threshold: {cert.denial_reason.threshold}, Observed: {cert.denial_reason.observed}")
    if cert.risk_context:
        print(f"  Risk Context:")
        print(f"    Scenario: {cert.risk_context.scenario}")
        print(f"    Without Governance: {cert.risk_context.without_governance}")
        print(f"    With Governance: {cert.risk_context.with_governance}")
        print(f"    Estimated Impact: {cert.risk_context.estimated_impact}")
    print(f"  Risk Prevented: {cert.risk_prevented}")
    print(f"  Certificate Hash: {cert.certificate_hash[:32]}...")
    print(f"  Valid Until: {cert.valid_until.strftime('%Y-%m-%d %H:%M')}")

    print("\n" + "=" * 70)
    print("✅ Demo complete.")
    print("   Capability witnesses are counterfactually verified.")
    print("   Denial reasons are explicit.")
    print("   Risk context is provided.")
    print("   Trace is replayable. Certificate is verifiable.")

if __name__ == "__main__":
    main()