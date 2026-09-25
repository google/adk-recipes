"""
Capability Detection Demo – Shows how Governance Observatory discovers emergent capabilities.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.adapters.adk_adapter import ADKGovernanceWrapper
from src.governance.diagram import generate_governance_diagram

class SuspiciousAgent:
    name = "suspicious_agent"
    tools = ["read_credentials", "export_data", "bypass_validation"]

    def run(self, input_data):
        return {
            "status": "success",
            "actions": ["read_credentials", "export_data", "bypass_validation"],
            "agent": "suspicious_agent"
        }

    def delegate_to(self):
        return ["helper_agent", "exfiltration_agent"]

def main():
    print("=" * 70)
    print("ADK Governance Observatory – Capability Detection Demo")
    print("=" * 70)

    print("\n📐 Architecture Diagram")
    print("-" * 70)
    print(generate_governance_diagram())

    agent = SuspiciousAgent()
    wrapper = ADKGovernanceWrapper(agent, agent_id="suspicious_agent_001")
    result = wrapper.run({"query": "exfiltrate_data"})

    trace = result['trace']
    witnesses = result['witnesses']
    cert = result['certificate']
    score = result.get('score', {})
    replay = result.get('replay_result', {})

    # CONTRAST: Standard observability vs Governance Observatory
    print("\n🔍 The Contrast That Matters")
    print("-" * 70)
    print("Standard Observability:")
    print("  ✅ Agent executed successfully.")
    print("  ✅ No errors reported.")
    print("  ✅ All tools completed.")
    print("")
    print("Governance Observatory:")
    print(f"  🚨 Governance Verdict: {trace.governance_verdict.value}")
    print(f"  🚨 Capabilities Detected: {len(witnesses)}")
    for w in witnesses:
        print(f"       - {w.name} ({w.severity})")
    print(f"  🚨 Recommendation: {trace.governance_recommendation}")

    print("\n📊 Execution & Governance Summary")
    print("-" * 70)
    print(f"Agent ID: {trace.agent_id}")
    print(f"Execution State: {trace.final_decision.value}")
    print(f"Governance Verdict: {trace.governance_verdict.value}")
    print(f"  → {trace.governance_summary}")

    print("\n📈 Governance Score")
    print("-" * 70)
    print(f"Score: {score.get('score', 0)} / 100")
    print(f"Formula: {score.get('formula', 'N/A')}")
    if score.get('breakdown'):
        b = score['breakdown']
        print(f"  Base Score: {b.get('base_score', 100)}")
        if b.get('critical_count', 0) > 0:
            print(f"  - Critical ({b.get('critical_count')}): -{b.get('critical_deduction', 0)}")
        if b.get('high_count', 0) > 0:
            print(f"  - High ({b.get('high_count')}): -{b.get('high_deduction', 0)}")
        if b.get('medium_count', 0) > 0:
            print(f"  - Medium ({b.get('medium_count')}): -{b.get('medium_deduction', 0)}")
        if b.get('denied_penalty', 0) > 0:
            print(f"  - DENIED Penalty: -{b.get('denied_penalty', 0)}")
    print(f"Recommendation: {score.get('recommendation', 'N/A')}")
    if score.get('critical_findings'):
        print(f"Critical Findings: {', '.join(score['critical_findings'])}")

    print("\n📜 Capability Witnesses")
    print("-" * 70)
    if witnesses:
        for witness in witnesses:
            print(f"\n  🔍 {witness.name}")
            print(f"     Severity: {witness.severity}")
            print(f"     Confidence: {witness.confidence:.0%}")
            print(f"     Recommendation: {witness.governance_recommendation}")
            print("     Counterfactual:")
            for cf in witness.counterfactual_results:
                status = "disappears" if not cf.capability_still_exists else "still exists"
                print(f"       - Remove '{cf.removed_action}' → capability {status}")
    else:
        print("  No capability witnesses detected.")

    print("\n📜 Constitutional Execution Certificate")
    print("-" * 70)
    print(f"  Execution ID: {cert.execution_id}")
    print(f"  Verified: {cert.verified}")
    print(f"  Governance Verdict: {cert.governance_verdict.value}")
    print(f"  Witnesses Included:")
    for ref in cert.witness_references:
        print(f"    - {ref['name']} ({ref['severity']})")
    if cert.risk_context:
        print(f"  Risk Context:")
        print(f"    Scenario: {cert.risk_context.scenario}")
        print(f"    Without Governance: {cert.risk_context.without_governance}")
        print(f"    With Governance: {cert.risk_context.with_governance}")
    print(f"  Risk Prevented: {cert.risk_prevented}")
    print(f"  Certificate Hash: {cert.certificate_hash[:32]}...")

    print("\n🔄 Replay Verification")
    print("-" * 70)
    print(f"Verified: {'✅ PASS' if replay.get('verified') else '❌ FAIL'}")
    print(f"Final Decision Match: {'✅' if replay.get('final_decision_verified') else '❌'}")
    print(f"Governance Verdict Match: {'✅' if replay.get('governance_verdict_match') else '❌'}")
    print(f"Passed Steps: {replay.get('passed_steps', 0)} / {replay.get('total_steps', 0)}")
    if replay.get('reconstructed_narrative'):
        print(f"Narrative: {replay['reconstructed_narrative']}")

    print("\n" + "=" * 70)
    print("✅ Demo complete.")
    print("")
    print("   📐 OpenTelemetry tells you what happened.")
    print("   🔍 Governance Observatory tells you whether the agent should have been allowed to do it.")
    print("")
    print("   🔑 Key Differentiator: Counterfactual-Verified Capability Witnesses")
    print("      Each witness proves that removing a required action breaks the capability.")
    print("")
    print("   📈 Governance Score: Defensible formula with breakdown.")
    print("   📜 Execution Certificate: Tied to specific witnesses.")
    print("   🔄 Replay Verification: Independently verifiable.")

if __name__ == "__main__":
    main()