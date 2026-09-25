import unittest
from src.governance.witness import WitnessEngine
from src.governance.models import GovernanceTrace, EvidenceState, AdmissibilityState

class TestWitness(unittest.TestCase):
    def test_witness_generation(self):
        engine = WitnessEngine()
        # Create a minimal trace
        trace = GovernanceTrace(
            trace_id="test-001",
            agent_id="test_agent",
            steps=[],
            final_decision=AdmissibilityState.ADMISSIBLE,
            final_reason="OK",
            delegation_chain=[],
            tools_called=[]
        )
        result = engine.generate_witness(trace)
        self.assertIn("witness_hash", result)

if __name__ == "__main__":
    unittest.main()