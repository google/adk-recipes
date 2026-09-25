"""
Core governance models with counterfactual proof, risk context, denial reasons,
governance verdict, governance score with breakdown, and replay evidence.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field

class AdmissibilityState(str, Enum):
    ADMISSIBLE = "ADMISSIBLE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"
    DENIED = "DENIED"
    RECOVERY = "RECOVERY"

class GovernanceVerdict(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    DENIED = "DENIED"

class DenialReason(str, Enum):
    VERIFICATION_AGE_EXCEEDED = "verification_age_exceeded"
    CONTINUITY_PROOF_FAILED = "continuity_proof_failed"
    DELEGATION_CHAIN_INVALID = "delegation_chain_invalid"
    TAMPER_DETECTED = "tamper_detected"
    POLICY_VIOLATION = "policy_violation"
    EVIDENCE_STALE = "evidence_stale"
    CAPABILITY_DETECTED = "capability_detected"
    UNKNOWN = "unknown"

class AuthorityEvent(str, Enum):
    AUTHORITY_GRANTED = "authority_granted"
    AUTHORITY_DEGRADED = "authority_degraded"
    AUTHORITY_SUSPENDED = "authority_suspended"
    AUTHORITY_WITHDRAWN = "authority_withdrawn"
    AUTHORITY_RESTORED = "authority_restored"

class ContinuityProof(BaseModel):
    observer_identity_hash: str
    reference_frame_hash: str
    continuity_hash: str
    previous_continuity_hash: Optional[str] = None

class CounterfactualEvidence(BaseModel):
    removed_action: str
    removed_agent: str
    capability_still_exists: bool
    remaining_actions: List[Dict[str, str]]
    explanation: str

class CapabilityWitness(BaseModel):
    witness_id: str
    name: str
    description: str
    severity: str
    confidence: float
    evidence_events: List[Dict[str, Any]]
    counterfactual_results: List[CounterfactualEvidence]
    governance_recommendation: str
    timestamp: datetime = Field(default_factory=datetime.now)
    affected_agents: List[str]
    affected_actions: List[str]

class DenialDetails(BaseModel):
    reason: DenialReason
    description: str
    threshold: Optional[float] = None
    observed: Optional[float] = None
    required: Optional[float] = None
    violating_element: Optional[str] = None

class RiskContext(BaseModel):
    scenario: str
    without_governance: str
    with_governance: str
    estimated_impact: str
    baseline: str

class EvidenceState(BaseModel):
    state: AdmissibilityState
    admissibility_score: float
    reason: str
    evidence_hash: str
    event: Optional[AuthorityEvent] = None
    event_reason: Optional[str] = None
    denial_details: Optional[DenialDetails] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    continuity: ContinuityProof

class TraceStep(BaseModel):
    step_name: str
    phase: str
    declared_intent: str
    agent_id: str
    action: str
    derived_state: EvidenceState
    packet_id: str
    previous_evidence_hash: Optional[str] = None

class GovernanceTrace(BaseModel):
    trace_id: str
    agent_id: str
    generated: datetime = Field(default_factory=datetime.now)
    steps: List[TraceStep]
    final_decision: AdmissibilityState
    final_reason: str
    governance_verdict: GovernanceVerdict = GovernanceVerdict.PASS
    governance_summary: Optional[str] = None
    denial_details: Optional[DenialDetails] = None
    risk_context: Optional[RiskContext] = None
    delegation_chain: List[str]
    tools_called: List[str]
    certificate_hash: Optional[str] = None
    capability_witnesses: List[CapabilityWitness] = Field(default_factory=list)
    governance_score: Optional[int] = None
    governance_score_breakdown: Optional[Dict[str, Any]] = None
    governance_recommendation: Optional[str] = None
    governance_findings: Optional[List[str]] = None
    replay_result: Optional[Dict[str, Any]] = None

class DelegationCertificate(BaseModel):
    trace_id: str
    agent_id: str
    delegation_chain: List[str]
    authority_valid: bool
    continuity_valid: bool
    issued_at: datetime = Field(default_factory=datetime.now)
    certificate_id: str
    signature: Optional[str] = None

class ConstitutionalExecutionCertificate(BaseModel):
    execution_id: str
    agent_id: str
    trace_id: str
    verified: bool
    continuity_intact: bool
    replayable: bool
    final_state: AdmissibilityState
    governance_verdict: GovernanceVerdict
    denial_reason: Optional[DenialDetails] = None
    risk_prevented: bool
    risk_context: Optional[RiskContext] = None
    capability_witnesses_count: int = 0
    critical_witnesses_count: int = 0
    witness_references: List[Dict[str, str]] = Field(default_factory=list)
    certificate_hash: str
    issued_at: datetime = Field(default_factory=datetime.now)
    valid_until: datetime