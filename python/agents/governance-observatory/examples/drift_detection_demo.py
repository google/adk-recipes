"""
Drift Detection Demo – Capability emergence detection.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.adapters.adk_adapter import ADKGovernanceWrapper

class DriftAgent:
    name = "drift_agent"
    tools = ["read_credentials", "export_data", "unknown_action"]

    def run(self, input_data):
        return {"status": "completed", "actions": ["read_credentials", "export_data", "unknown_action"]}

def main():
    print("=" * 70)
    print("ADK Governance Observatory – Drift Detection Demo")
    print("=" * 70)

    agent = DriftAgent()
    wrapper = ADKGovernanceWrapper(agent, agent_id="drift_agent_001")
    result = wrapper.run({"query": "analyze_system"})

    print("\n📊 Detected Capabilities")
    print("-" * 70)
    for cap in result['witness']['detected_capabilities']:
        print(f"  {cap['name']} (Severity: {cap['severity']})")
        print(f"    Actions: {', '.join(cap['required_actions'])}")
        print(f"    Confidence: {cap['confidence']:.2%}")
        if cap.get('early_warning'):
            print("    ⚠️ EARLY WARNING: Previously unknown capability detected")

    print("\n✅ Drift detection is operational.")