"""Basic evalualtion for Image Scoring."""

import pathlib

import dotenv
import pytest
from google.adk.evaluation import AgentEvaluator
from google.adk.evaluation.custom_metric_evaluator import _CustomMetricEvaluator
from google.adk.evaluation.metric_evaluator_registry import (
    DEFAULT_METRIC_EVALUATOR_REGISTRY,
)
from google.adk.evaluation.metric_info_providers import (
    TrajectoryEvaluatorMetricInfoProvider,
)

pytest_plugins = ("pytest_asyncio",)

# Override the default evaluator for tool_trajectory_avg_score
DEFAULT_METRIC_EVALUATOR_REGISTRY.register_evaluator(
    metric_info=TrajectoryEvaluatorMetricInfoProvider().get_metric_info(),
    evaluator=_CustomMetricEvaluator,
)


@pytest.fixture(scope="session", autouse=True)
def load_env():
    dotenv.load_dotenv()


@pytest.mark.asyncio
async def test_all():
    """Test the agent's basic ability on a few examples."""
    test_json_path = str(
        pathlib.Path(__file__).parent / "data" / "on_brand_genmedia.test.json"
    )
    print(f"\nLooking for evalset at: {test_json_path}")
    results = await AgentEvaluator.evaluate(
        "on_brand_genmedia",
        str(pathlib.Path(__file__).parent / "data"),
        num_runs=1,
    )
    if not results:
        print(
            "WARNING: AgentEvaluator returned no results. It likely found 0 valid test cases."
        )
    else:
        print(f"\nSUCCESS: AgentEvaluator parsed and ran {len(results)} eval runs.")
        for r in results:
            print(f"Test Case: {r.eval_case.eval_id} | Metrics: {r.metrics}")
