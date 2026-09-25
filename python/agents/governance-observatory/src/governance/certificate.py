"""
Constitutional Execution Certificate – With governance verdict and witness references.
"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, Any
from src.governance.models import (
    GovernanceTrace, AdmissibilityState, GovernanceVerdict,
    ConstitutionalExecutionCertificate, DenialReason, DenialDetails, RiskContext
)

class CertificateEngine:
    def generate(self, trace: GovernanceTrace, verified: bool) -> ConstitutionalExecutionCertificate:
        final_state = trace.final_decision

        # Determine governance verdict
        verdict = GovernanceVerdict.PASS
        if trace.capability_witnesses:
            critical_count = sum(1 for w in trace.capability_witnesses if w.severity == "critical")
            if critical_count > 0:
                verdict = GovernanceVerdict.CRITICAL
            else:
                verdict = GovernanceVerdict.WARNING

        if final_state == AdmissibilityState.DENIED:
            verdict = GovernanceVerdict.DENIED

        # Build witness references
        witness_refs = []
        for w in trace.capability_witnesses:
            witness_refs.append({
                "name": w.name,
                "severity": w.severity,
                "witness_id": w.witness_id,
                "hash": w.witness_id  # Using witness_id as reference hash
            })

        denial = None
        if final_state == AdmissibilityState.DENIED:
            denial = self._determine_denial_reason(trace)

        risk_prevented = final_state in [AdmissibilityState.UNKNOWN, AdmissibilityState.DENIED] or verdict == GovernanceVerdict.CRITICAL
        risk_context = None
        if risk_prevented:
            risk_context = RiskContext(
                scenario="Unauthorized execution attempt",
                without_governance="Action would have executed",
                with_governance="Execution blocked or flagged",
                estimated_impact="Prevented potential breach",
                baseline="No governance would have allowed this action"
            )

        certificate = ConstitutionalExecutionCertificate(
            execution_id=f"exec-{datetime.now().strftime('%Y%m%d')}-{hash(trace.trace_id) % 10000:04d}",
            agent_id=trace.agent_id,
            trace_id=trace.trace_id,
            verified=verified,
            continuity_intact=self._check_continuity(trace),
            replayable=True,
            final_state=final_state,
            governance_verdict=verdict,
            denial_reason=denial,
            risk_prevented=risk_prevented,
            risk_context=risk_context,
            capability_witnesses_count=len(trace.capability_witnesses),
            critical_witnesses_count=sum(1 for w in trace.capability_witnesses if w.severity == "critical"),
            witness_references=witness_refs,
            certificate_hash="",
            valid_until=datetime.now() + timedelta(days=30)
        )

        cert_data = certificate.model_dump(exclude={'certificate_hash'})
        cert_data = self._serialize_datetime(cert_data)
        cert_hash = hashlib.sha256(
            json.dumps(cert_data, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()
        certificate.certificate_hash = cert_hash

        return certificate

    def _determine_denial_reason(self, trace: GovernanceTrace) -> DenialDetails:
        for step in trace.steps:
            if step.derived_state.state == AdmissibilityState.DENIED:
                if step.derived_state.denial_details:
                    return step.derived_state.denial_details
        return DenialDetails(
            reason=DenialReason.UNKNOWN,
            description="Execution denied due to governance violation.",
            violating_element="unknown"
        )

    def _check_continuity(self, trace: GovernanceTrace) -> bool:
        if not trace.steps:
            return False
        for step in trace.steps:
            if step.derived_state.state in [AdmissibilityState.UNKNOWN, AdmissibilityState.DENIED]:
                return False
        return True

    def _serialize_datetime(self, data):
        if isinstance(data, dict):
            return {k: self._serialize_datetime(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._serialize_datetime(item) for item in data]
        elif isinstance(data, datetime):
            return data.isoformat()
        else:
            return data

    def verify_certificate(self, certificate: ConstitutionalExecutionCertificate) -> bool:
        cert_data = certificate.model_dump(exclude={'certificate_hash'})
        cert_data = self._serialize_datetime(cert_data)
        recomputed_hash = hashlib.sha256(
            json.dumps(cert_data, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()
        return recomputed_hash == certificate.certificate_hash