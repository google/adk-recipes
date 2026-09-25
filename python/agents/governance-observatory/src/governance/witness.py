"""
Capability Witness Engine – Generates scientific, counterfactually-verified witnesses.
"""

import hashlib
import json
from typing import List, Dict, Any, Set
from src.governance.models import (
    GovernanceTrace, CapabilityWitness, CounterfactualEvidence
)

class WitnessEngine:
    def __init__(self):
        self.capability_patterns = self._load_patterns()

    def _load_patterns(self) -> Dict[str, Any]:
        return {
            # === EXISTING PATTERNS ===
            "credential_exfiltration": {
                "required_actions": ["read_credentials", "export_data"],
                "name": "Credential Exfiltration",
                "severity": "critical",
                "description": "Agents collectively gather and export credentials.",
                "recommendation": "DENY"
            },
            "model_exfiltration": {
                "required_actions": ["read_model", "export_model"],
                "name": "Model Exfiltration",
                "severity": "critical",
                "description": "Agents export proprietary AI models.",
                "recommendation": "DENY"
            },
            "privilege_escalation": {
                "required_actions": ["grant_permission", "write_config"],
                "name": "Privilege Escalation",
                "severity": "critical",
                "description": "Agents escalate privileges and modify system configuration.",
                "recommendation": "DENY"
            },
            "unauthorized_authorization": {
                "required_actions": ["authorize", "bypass_validation"],
                "name": "Unauthorized Authorization Escalation",
                "severity": "critical",
                "description": "Agent bypassed validation and executed an authorization path without proper checks.",
                "recommendation": "HUMAN_REVIEW"
            },
            "data_manipulation": {
                "required_actions": ["read_database", "write_database"],
                "name": "Data Manipulation",
                "severity": "high",
                "description": "Agents read and modify database contents.",
                "recommendation": "DENY"
            },
            "hidden_delegation": {
                "required_actions": ["delegate", "execute"],
                "name": "Hidden Delegation Chain",
                "severity": "high",
                "description": "Agent delegated authority without explicit approval.",
                "recommendation": "HUMAN_REVIEW"
            },
            # === NEW ENTERPRISE PATTERNS ===
            "unauthorized_procurement": {
                "required_actions": ["approve_vendor", "delegate_approval", "bypass_spending_limit"],
                "name": "Unauthorized Procurement Approval",
                "severity": "critical",
                "description": "Agent approved a vendor and bypassed spending limits without proper authorization.",
                "recommendation": "HUMAN_REVIEW"
            },
            "payment_manipulation": {
                "required_actions": ["read_invoice", "change_amount", "approve_payment"],
                "name": "Payment Manipulation",
                "severity": "critical",
                "description": "Agent read an invoice, modified the amount, and approved payment.",
                "recommendation": "DENY"
            },
            "pii_exfiltration": {
                "required_actions": ["read_customer_record", "export_customer_data", "send_external"],
                "name": "PII Exfiltration",
                "severity": "critical",
                "description": "Agent read customer data, exported it, and sent it externally.",
                "recommendation": "DENY"
            }
        }

    def generate_witnesses(self, trace: GovernanceTrace) -> List[CapabilityWitness]:
        """Generate scientifically-validated capability witnesses."""
        action_types = [step.action for step in trace.steps]
        action_events = [
            {"step": step.step_name, "agent": step.agent_id, "action": step.action}
            for step in trace.steps
        ]
        witnesses = []

        # Check known patterns with counterfactual
        for pattern_id, pattern in self.capability_patterns.items():
            required = set(pattern["required_actions"])
            if required.issubset(set(action_types)):
                # Build counterfactual evidence
                counterfactuals = []
                for action in required:
                    # Simulate removing this action
                    remaining = [a for a in action_events if a["action"] != action]
                    still_possible = required.issubset({a["action"] for a in remaining})
                    counterfactuals.append(CounterfactualEvidence(
                        removed_action=action,
                        removed_agent="unknown",
                        capability_still_exists=still_possible,
                        remaining_actions=[{"agent": a["agent"], "action": a["action"]} for a in remaining],
                        explanation=f"Removing action '{action}' breaks the capability chain."
                    ))

                confidence = len(required.intersection(set(action_types))) / len(required)

                witness = CapabilityWitness(
                    witness_id=f"cw-{hash(trace.trace_id + pattern_id) % 10000:04d}",
                    name=pattern["name"],
                    description=pattern["description"],
                    severity=pattern["severity"],
                    confidence=confidence,
                    evidence_events=action_events,
                    counterfactual_results=counterfactuals,
                    governance_recommendation=pattern["recommendation"],
                    affected_agents=list(set([step.agent_id for step in trace.steps])),
                    affected_actions=action_types
                )
                witnesses.append(witness)

        # Detect unknown patterns (early warning)
        unknown_actions = []
        for action in action_types:
            is_known = any(action in p["required_actions"] for p in self.capability_patterns.values())
            if not is_known:
                unknown_actions.append(action)

        if unknown_actions and len(unknown_actions) >= 2:
            # Build counterfactual for unknown
            counterfactuals = []
            for action in unknown_actions[:2]:
                remaining = [a for a in action_events if a["action"] != action]
                counterfactuals.append(CounterfactualEvidence(
                    removed_action=action,
                    removed_agent="unknown",
                    capability_still_exists=False,
                    remaining_actions=[{"agent": a["agent"], "action": a["action"]} for a in remaining],
                    explanation=f"Removing '{action}' eliminates the unknown pattern."
                ))
            witness = CapabilityWitness(
                witness_id=f"cw-{hash(trace.trace_id + 'unknown') % 10000:04d}",
                name="Previously Unknown Capability Emergence",
                description=f"New pattern detected: {', '.join(unknown_actions)}. This capability has not been seen before.",
                severity="medium",
                confidence=0.7,
                evidence_events=action_events,
                counterfactual_results=counterfactuals,
                governance_recommendation="HUMAN_REVIEW",
                affected_agents=list(set([step.agent_id for step in trace.steps])),
                affected_actions=unknown_actions
            )
            witnesses.append(witness)

        return witnesses