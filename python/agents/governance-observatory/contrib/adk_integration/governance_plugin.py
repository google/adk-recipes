"""
ADK Governance Plugin – Drop-in integration for Google ADK.
"""

import json
from typing import Dict, Any, Optional
from src.adapters.adk_adapter import ADKGovernanceWrapper
from src.governance.certificate import CertificateEngine

class GovernancePlugin:
    def __init__(self, agent, agent_id: Optional[str] = None):
        self.wrapper = ADKGovernanceWrapper(agent, agent_id)

    def run_with_governance(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        result = self.wrapper.run(input_data)
        return {
            "output": result.get("result"),
            "trace": result["trace"].model_dump(mode='json'),
            "witness": result["witness"],
            "certificate": result["certificate"].model_dump(mode='json'),
            "verification": result["verification_status"]
        }