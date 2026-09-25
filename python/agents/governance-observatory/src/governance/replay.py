"""
Replay Engine – Independently replays a governance trace and produces verifiable evidence.
"""

import hashlib
import json
from typing import Dict, Any
from src.governance.models import GovernanceTrace, GovernanceVerdict

class ReplayEngine:
    """Replays a governance trace to verify correctness."""

    def _compute_hash(self, agent_id: str, action: str, state: str, score: float, reason: str) -> str:
        data = {
            "agent": agent_id,
            "action": action,
            "state": state,
            "score": score,
            "reason": reason
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()

    def replay(self, trace: GovernanceTrace) -> Dict[str, Any]:
        report = {
            "trace_id": trace.trace_id,
            "verified": True,
            "issues": [],
            "steps": [],
            "final_decision_verified": False,
            "governance_verdict_match": False,
            "witness_match": False,
            "total_steps": 0,
            "passed_steps": 0,
            "replay_integrity": "PARTIAL"
        }

        previous_hash = ""
        for i, step in enumerate(trace.steps):
            step_report = {
                "step": step.step_name,
                "expected_state": step.derived_state.state.value,
                "passed": True
            }

            recomputed_hash = self._compute_hash(
                step.agent_id,
                step.action,
                step.derived_state.state.value,
                step.derived_state.admissibility_score,
                step.derived_state.reason
            )

            if recomputed_hash != step.derived_state.evidence_hash:
                step_report["passed"] = False
                step_report["issue"] = "Evidence hash mismatch"
                report["issues"].append(f"Step {i+1}: Hash mismatch")
                report["verified"] = False

            if previous_hash and step.previous_evidence_hash != previous_hash:
                step_report["passed"] = False
                step_report["issue"] = "Chain break"
                report["issues"].append(f"Step {i+1}: Chain break")
                report["verified"] = False

            step_report["recomputed_hash"] = recomputed_hash
            report["steps"].append(step_report)
            previous_hash = recomputed_hash

        report["total_steps"] = len(trace.steps)
        report["passed_steps"] = sum(1 for s in report["steps"] if s["passed"])

        if trace.steps and trace.steps[-1].derived_state.state == trace.final_decision:
            report["final_decision_verified"] = True

        if trace.capability_witnesses:
            critical_count = sum(1 for w in trace.capability_witnesses if w.severity == "critical")
            expected_verdict = GovernanceVerdict.CRITICAL if critical_count > 0 else GovernanceVerdict.WARNING
        else:
            expected_verdict = GovernanceVerdict.PASS

        if trace.governance_verdict == expected_verdict:
            report["governance_verdict_match"] = True
        else:
            report["governance_verdict_match"] = False
            report["verified"] = False

        if trace.capability_witnesses:
            report["witness_match"] = True
        else:
            report["witness_match"] = True

        # Determine verification status (single icon)
        if (report["final_decision_verified"] and
            report["governance_verdict_match"] and
            report["passed_steps"] == report["total_steps"] and
            report["total_steps"] > 0 and
            not report["issues"]):
            report["verified"] = True
            report["replay_integrity"] = "VERIFIED"
        elif report["passed_steps"] > 0:
            report["verified"] = False
            report["replay_integrity"] = "PARTIAL"
        else:
            report["verified"] = False
            report["replay_integrity"] = "FAILED"

        report["reconstructed_narrative"] = self._reconstruct_narrative(trace)
        return report

    def _reconstruct_narrative(self, trace: GovernanceTrace) -> str:
        if not trace.steps:
            return "No steps recorded."
        narrative = f"Agent {trace.agent_id} attempted execution. "
        if trace.capability_witnesses:
            witness_names = [w.name for w in trace.capability_witnesses]
            narrative += f"Capability witnesses detected: {', '.join(witness_names)}. "
        if trace.governance_verdict == GovernanceVerdict.CRITICAL:
            narrative += "Critical governance findings detected. Execution should be blocked or reviewed."
        elif trace.governance_verdict == GovernanceVerdict.PASS:
            narrative += "No governance violations detected. Execution passed."
        else:
            narrative += f"Governance verdict: {trace.governance_verdict.value}."
        return narrative