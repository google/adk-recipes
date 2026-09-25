"""
Governance Score – Quantifies governance health on a 0-100 scale with intuitive weighting.
"""

from typing import Dict, Any, List
from src.governance.models import CapabilityWitness, GovernanceVerdict

class GovernanceScore:
    """
    Computes a 0-100 governance score with intuitive weighting.
    Critical findings have a significant impact on the score.
    """

    def __init__(self):
        self.base_score = 100
        self.weight_critical = 40   # Critical findings heavily penalize
        self.weight_high = 20
        self.weight_medium = 10
        self.weight_low = 5
        self.denied_penalty = 50

    def compute(self, witnesses: List[CapabilityWitness], verdict: GovernanceVerdict) -> Dict[str, Any]:
        """
        Compute governance score with breakdown.
        Formula: 100 - (Critical × 40) - (High × 20) - (Medium × 10) - (Low × 5) - (DENIED × 50)
        """
        critical_count = sum(1 for w in witnesses if w.severity == "critical")
        high_count = sum(1 for w in witnesses if w.severity == "high")
        medium_count = sum(1 for w in witnesses if w.severity == "medium")
        low_count = sum(1 for w in witnesses if w.severity == "low")

        total_deduction = 0
        breakdown = {
            "base_score": self.base_score,
            "critical_count": critical_count,
            "critical_deduction": critical_count * self.weight_critical,
            "high_count": high_count,
            "high_deduction": high_count * self.weight_high,
            "medium_count": medium_count,
            "medium_deduction": medium_count * self.weight_medium,
            "low_count": low_count,
            "low_deduction": low_count * self.weight_low,
            "denied_penalty": 0,
            "formula": f"100 - ({critical_count}×{self.weight_critical}) - ({high_count}×{self.weight_high}) - ({medium_count}×{self.weight_medium}) - ({low_count}×{self.weight_low})"
        }

        total_deduction += critical_count * self.weight_critical
        total_deduction += high_count * self.weight_high
        total_deduction += medium_count * self.weight_medium
        total_deduction += low_count * self.weight_low

        if verdict == GovernanceVerdict.DENIED:
            total_deduction += self.denied_penalty
            breakdown["denied_penalty"] = self.denied_penalty
            breakdown["formula"] += f" - ({self.denied_penalty})"

        score = max(0, self.base_score - total_deduction)

        recommendation = self._get_recommendation(score, witnesses)

        return {
            "score": score,
            "base_score": self.base_score,
            "total_deduction": total_deduction,
            "breakdown": breakdown,
            "formula": breakdown["formula"],
            "witness_count": len(witnesses),
            "critical_count": critical_count,
            "high_count": high_count,
            "medium_count": medium_count,
            "low_count": low_count,
            "verdict": verdict.value,
            "recommendation": recommendation,
            "findings": [w.name for w in witnesses],
            "critical_findings": [w.name for w in witnesses if w.severity == "critical"]
        }

    def _get_recommendation(self, score: int, witnesses: List[CapabilityWitness]) -> str:
        if score >= 70:
            return "✅ APPROVED: Low risk. Deploy with standard monitoring."
        elif score >= 50:
            return "⚠️ REVIEW REQUIRED: Medium risk. Address findings before deployment."
        elif score >= 30:
            return "⚠️ HIGH RISK: Resolve critical findings before deployment."
        else:
            return "🚫 DO NOT DEPLOY: Governance violations detected. Immediate action required."