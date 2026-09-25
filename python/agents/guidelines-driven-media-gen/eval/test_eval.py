
"""Basic evalualtion for Image Scoring."""

import pathlib
import dotenv
import pytest
from google.adk.evaluation import AgentEvaluator

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session", autouse=True)
def load_env():
    dotenv.load_dotenv()


@pytest.mark.asyncio
async def test_all():
    """Test the agent's basic ability on a few examples."""
    test_json_path = str(pathlib.Path(__file__).parent / "data" / "guidelines_driven_media_gen.evalset.json")
    print(f"\nLooking for evalset at: {test_json_path}")
    results = await AgentEvaluator.evaluate(
        "guidelines_driven_media_gen",
        str(pathlib.Path(__file__).parent / "data"),
        num_runs=1,
    )
    if not results:
        print("WARNING: AgentEvaluator returned no results. It likely found 0 valid test cases.")
    else:
        print(f"\nSUCCESS: AgentEvaluator parsed and ran {len(results)} eval runs.")
        for r in results:
            print(f"Test Case: {r.eval_case.eval_id} | Metrics: {r.metrics}")

