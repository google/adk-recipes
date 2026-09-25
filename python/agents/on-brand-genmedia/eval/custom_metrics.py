import logging

from google.adk.evaluation.eval_case import Invocation, get_all_tool_calls
from google.adk.evaluation.eval_metrics import EvalMetric
from google.adk.evaluation.evaluator import (
    EvalStatus,
    EvaluationResult,
    PerInvocationResult,
)

logger = logging.getLogger(__name__)


def name_only_trajectory_evaluator(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None = None,
    conversation_scenario=None,
) -> EvaluationResult:
    """Evaluates tool use trajectories by matching tool names only, ignoring arguments."""
    logger.info("Running name_only_trajectory_evaluator")
    if not expected_invocations:
        logger.warning("No expected invocations provided for trajectory evaluation.")
        return EvaluationResult(overall_eval_status=EvalStatus.NOT_EVALUATED)

    threshold = eval_metric.threshold if eval_metric.threshold is not None else 1.0

    total_tool_use_accuracy = 0.0
    num_invocations = 0
    per_invocation_results = []

    for actual, expected in zip(actual_invocations, expected_invocations, strict=False):
        actual_tool_uses = get_all_tool_calls(actual.intermediate_data)
        expected_tool_uses = get_all_tool_calls(expected.intermediate_data)

        actual_names = [t.name for t in actual_tool_uses]
        expected_names = [t.name for t in expected_tool_uses]

        logger.info(f"Actual tool names: {actual_names}")
        logger.info(f"Expected tool names: {expected_names}")

        # ANY_ORDER match logic for names
        match_status = False
        if len(actual_names) >= len(expected_names):
            actual_copy = list(actual_names)
            found_all = True
            for exp in expected_names:
                if exp in actual_copy:
                    actual_copy.remove(exp)
                else:
                    found_all = False
                    break
            match_status = found_all

        score = 1.0 if match_status else 0.0

        eval_status = EvalStatus.PASSED if score >= threshold else EvalStatus.FAILED

        per_invocation_results.append(
            PerInvocationResult(
                actual_invocation=actual,
                expected_invocation=expected,
                score=score,
                eval_status=eval_status,
            )
        )
        total_tool_use_accuracy += score
        num_invocations += 1

    overall_score = (
        total_tool_use_accuracy / num_invocations if num_invocations > 0 else 0.0
    )
    overall_eval_status = (
        EvalStatus.PASSED if overall_score >= threshold else EvalStatus.FAILED
    )

    logger.info(f"Overall score: {overall_score}, Status: {overall_eval_status}")

    return EvaluationResult(
        overall_score=overall_score,
        overall_eval_status=overall_eval_status,
        per_invocation_results=per_invocation_results,
    )
