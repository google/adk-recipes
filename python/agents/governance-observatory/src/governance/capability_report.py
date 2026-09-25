"""
Capability Witness Report – Generates a markdown/JSON report.
"""

import json
from datetime import datetime
from typing import List, Dict, Any
from src.governance.models import CapabilityWitness

class CapabilityReport:
    def __init__(self, witnesses: List[CapabilityWitness]):
        self.witnesses = witnesses

    def to_markdown(self) -> str:
        lines = []
        lines.append("# Capability Witness Report")
        lines.append("")
        lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"**Total Witnesses:** {len(self.witnesses)}")
        lines.append("")
        critical = [w for w in self.witnesses if w.severity == "critical"]
        if critical:
            lines.append("## Critical Findings")
            for w in critical:
                lines.append(f"- **{w.name}** (Confidence: {w.confidence:.0%})")
                lines.append(f"  - Recommendation: {w.governance_recommendation}")
                lines.append(f"  - Counterfactual: {'; '.join([f'remove {cf.removed_action} → capability disappears' for cf in w.counterfactual_results])}")
            lines.append("")
        lines.append("## All Witnesses")
        for w in self.witnesses:
            lines.append(f"### {w.name}")
            lines.append(f"- Severity: {w.severity}")
            lines.append(f"- Confidence: {w.confidence:.0%}")
            lines.append(f"- Recommendation: {w.governance_recommendation}")
            lines.append(f"- Affected Agents: {', '.join(w.affected_agents)}")
            lines.append("")
        return "\n".join(lines)