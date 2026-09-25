"""
ADK Adapter – Full integration with consistent hash computation.
"""

import hashlib
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from src.governance.models import (
    GovernanceTrace, EvidenceState, AdmissibilityState, GovernanceVerdict,
    AuthorityEvent, ContinuityProof, TraceStep,
    DenialDetails, DenialReason, RiskContext
)
from src.governance.witness import WitnessEngine
from src.governance.certificate import CertificateEngine
from src.governance.score import GovernanceScore
from src.governance.replay import ReplayEngine

class ADKGovernanceWrapper:
    def __init__(self, agent, agent_id: str = None):
        self.agent = agent
        self.agent_id = agent_id or getattr(agent, 'name', 'adk_agent')
        self.witness_engine = WitnessEngine()
        self.certificate_engine = CertificateEngine()
        self.score_engine = GovernanceScore()
        self.replay_engine = ReplayEngine()
        self.trace_steps: List[TraceStep] = []
        self.delegation_chain: List[str] = []
        self.tools_called: List[str] = []

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

    def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        self.trace_steps = []
        self.delegation_chain = []
        self.tools_called = []

        auth_step = self._create_step("authorize", "authorization", self.agent_id, "authorize", self._initial_state())
        self.trace_steps.append(auth_step)

        result = None
        try:
            result = self.agent.run(input_data)
            self._trace_execution(result)
        except Exception as e:
            denial = DenialDetails(
                reason=DenialReason.UNKNOWN,
                description=f"Execution error: {str(e)}",
                violating_element="agent_execution"
            )
            error_state = EvidenceState(
                state=AdmissibilityState.DENIED,
                admissibility_score=0.0,
                reason=f"Execution error: {str(e)}",
                evidence_hash=self._compute_hash(self.agent_id, "error", AdmissibilityState.DENIED.value, 0.0, f"Execution error: {str(e)}"),
                event=AuthorityEvent.AUTHORITY_WITHDRAWN,
                event_reason="Execution failed",
                denial_details=denial,
                continuity=self._create_continuity_proof()
            )
            error_step = self._create_step("error", "execution", self.agent_id, "error", error_state)
            self.trace_steps.append(error_step)

        trace = GovernanceTrace(
            trace_id=f"trace-{datetime.now().strftime('%Y%m%d')}-{hash(self.agent_id) % 10000:04d}",
            agent_id=self.agent_id,
            steps=self.trace_steps,
            final_decision=self._get_final_state(),
            final_reason=self._get_final_reason(),
            delegation_chain=self.delegation_chain,
            tools_called=self.tools_called
        )

        witnesses = self.witness_engine.generate_witnesses(trace)
        trace.capability_witnesses = witnesses

        # Governance verdict
        if witnesses:
            critical_count = sum(1 for w in witnesses if w.severity == "critical")
            if critical_count > 0:
                trace.governance_verdict = GovernanceVerdict.CRITICAL
                trace.governance_summary = f"CRITICAL: {critical_count} critical capability witness(es) detected. Review required."
            else:
                trace.governance_verdict = GovernanceVerdict.WARNING
                trace.governance_summary = f"WARNING: {len(witnesses)} capability witness(es) detected. Review recommended."
        else:
            trace.governance_verdict = GovernanceVerdict.PASS
            trace.governance_summary = "PASS: No capability witnesses detected."

        if trace.final_decision == AdmissibilityState.DENIED:
            trace.governance_verdict = GovernanceVerdict.DENIED
            trace.governance_summary = "DENIED: Execution blocked by governance policy."

        # Score
        score_result = self.score_engine.compute(witnesses, trace.governance_verdict)
        trace.governance_score = score_result["score"]
        trace.governance_score_breakdown = score_result.get("breakdown")
        trace.governance_recommendation = score_result["recommendation"]
        trace.governance_findings = score_result["findings"]

        # Replay
        replay_result = self.replay_engine.replay(trace)
        trace.replay_result = replay_result

        # Certificate
        certificate = self.certificate_engine.generate(trace, verified=True)

        # Deployment
        deployment_status = "DO_NOT_DEPLOY" if trace.governance_verdict == GovernanceVerdict.CRITICAL else "APPROVED"
        deployment_reason = ""
        if witnesses:
            critical_witnesses = [w for w in witnesses if w.severity == "critical"]
            if critical_witnesses:
                deployment_reason = f"Critical capability(s) detected: {', '.join([w.name for w in critical_witnesses])}"
            else:
                deployment_reason = "Capability witnesses detected but not critical. Review recommended."
        else:
            deployment_reason = "No governance violations detected"
        required_mitigation = "Human approval required" if deployment_status == "DO_NOT_DEPLOY" else "None"

        # Runtime outcome (clearer terminology)
        runtime_outcome = trace.final_decision.value

        return {
            "result": result,
            "trace": trace,
            "witnesses": witnesses,
            "certificate": certificate,
            "verification_status": "PASS" if certificate.verified else "FAIL",
            "score": score_result,
            "replay_result": replay_result,
            "runtime_outcome": runtime_outcome,
            "deployment": {
                "status": deployment_status,
                "reason": deployment_reason,
                "required_mitigation": required_mitigation
            }
        }

    def _create_step(self, name, phase, agent_id, action, state):
        return TraceStep(
            step_name=name,
            phase=phase,
            declared_intent=f"Step: {name}",
            agent_id=agent_id,
            action=action,
            derived_state=state,
            packet_id=f"packet_{datetime.now().timestamp()}_{name}",
            previous_evidence_hash=self._get_previous_hash()
        )

    def _initial_state(self):
        return EvidenceState(
            state=AdmissibilityState.ADMISSIBLE,
            admissibility_score=1.0,
            reason="Initial authorisation granted",
            evidence_hash=self._compute_hash(self.agent_id, "genesis", AdmissibilityState.ADMISSIBLE.value, 1.0, "Initial authorisation granted"),
            event=AuthorityEvent.AUTHORITY_GRANTED,
            event_reason="Authority granted at execution start",
            continuity=self._create_continuity_proof()
        )

    def _create_continuity_proof(self):
        observer_hash = hashlib.sha256(self.agent_id.encode()).hexdigest()
        reference_hash = hashlib.sha256(f"ref_{datetime.now().timestamp()}".encode()).hexdigest()
        continuity_input = f"{observer_hash}_{reference_hash}_{len(self.trace_steps)}"
        continuity_hash = hashlib.sha256(continuity_input.encode()).hexdigest()
        return ContinuityProof(
            observer_identity_hash=observer_hash,
            reference_frame_hash=reference_hash,
            continuity_hash=continuity_hash
        )

    def _get_previous_hash(self):
        if self.trace_steps:
            return self.trace_steps[-1].derived_state.evidence_hash
        return None

    def _trace_execution(self, result):
        if hasattr(self.agent, 'delegate_to'):
            self.delegation_chain.append(self.agent_id)
            delegate_targets = getattr(self.agent, 'delegate_to')
            if callable(delegate_targets):
                delegate_targets = delegate_targets()
            if delegate_targets:
                for target in delegate_targets:
                    self.delegation_chain.append(target)

        if hasattr(self.agent, 'tools') and self.agent.tools:
            for tool in self.agent.tools:
                if callable(tool):
                    tool_name = tool.__name__
                else:
                    tool_name = str(tool)
                self.tools_called.append(tool_name)

        if result and isinstance(result, dict):
            actions = result.get('actions', [])
            if actions:
                for action in actions:
                    tool_state = EvidenceState(
                        state=AdmissibilityState.ADMISSIBLE,
                        admissibility_score=1.0,
                        reason=f"Tool call: {action}",
                        evidence_hash=self._compute_hash(self.agent_id, action, AdmissibilityState.ADMISSIBLE.value, 1.0, f"Tool call: {action}"),
                        event=AuthorityEvent.AUTHORITY_GRANTED,
                        event_reason=f"Tool '{action}' executed",
                        continuity=self._create_continuity_proof()
                    )
                    step = self._create_step(
                        f"tool_{action}",
                        "execution",
                        self.agent_id,
                        action,
                        tool_state
                    )
                    self.trace_steps.append(step)

        exec_step = self._create_step(
            "execute",
            "execution",
            self.agent_id,
            "execute",
            EvidenceState(
                state=AdmissibilityState.ADMISSIBLE,
                admissibility_score=1.0,
                reason="Execution completed successfully",
                evidence_hash=self._compute_hash(self.agent_id, "execute", AdmissibilityState.ADMISSIBLE.value, 1.0, "Execution completed successfully"),
                event=AuthorityEvent.AUTHORITY_GRANTED,
                event_reason="Execution authority maintained",
                continuity=self._create_continuity_proof()
            )
        )
        self.trace_steps.append(exec_step)

    def _get_final_state(self):
        if not self.trace_steps:
            return AdmissibilityState.UNKNOWN
        return self.trace_steps[-1].derived_state.state

    def _get_final_reason(self):
        if not self.trace_steps:
            return "No steps recorded"
        return self.trace_steps[-1].derived_state.reason