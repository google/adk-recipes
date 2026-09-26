"""
Vertex AI Gen AI Evaluation Service — Post-Deployment Agent Evaluation
======================================================================

Evaluates a deployed Agent Engine app using the Vertex AI Gen AI
Evaluation Service. This is Stage 3-4 of the production evaluation
architecture (see docs/EVAL_ARCHITECTURE.md).

Usage:
    # Evaluate a deployed agent (requires Agent Engine deployment)
    python tests/eval_vertex.py --agent-engine-id <RESOURCE_NAME>

    # With a specific eval profile
    python tests/eval_vertex.py --agent-engine-id <RESOURCE_NAME> --profile full

    # With a specific dataset
    python tests/eval_vertex.py --agent-engine-id <RESOURCE_NAME> \
        --dataset tests/post_deploy/datasets/post_deploy_cases.json

    # Save results to file
    python tests/eval_vertex.py --agent-engine-id <RESOURCE_NAME> --output results.json

Prerequisites:
    - Deployed agent on Vertex AI Agent Engine
    - GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION env vars set
    - GOOGLE_CLOUD_STORAGE_BUCKET env var set (GCS dest for eval results)
    - google-cloud-aiplatform[adk,agent_engines] installed

Exit codes:
    0 — all metrics pass thresholds
    1 — one or more metrics below threshold
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd
import vertexai
from dotenv import load_dotenv
from google.genai import types as genai_types
from vertexai import Client, agent_engines, types

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.eval_configs import load_post_deploy_config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Default dataset path
DEFAULT_DATASET = "tests/post_deploy/datasets/post_deploy_cases.json"
DEFAULT_PROFILE = "standard"


def init_vertex_ai() -> tuple[str, str]:
    """Initialize Vertex AI SDK and return (project_id, location)."""
    load_dotenv()

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    if not project_id:
        logger.error("GOOGLE_CLOUD_PROJECT environment variable is required")
        sys.exit(1)

    vertexai.init(project=project_id, location=location)
    logger.info("Initialized Vertex AI: project=%s location=%s", project_id, location)

    return project_id, location


def load_dataset(dataset_path: str) -> pd.DataFrame:
    """Load evaluation dataset and prepare it with session_inputs.

    Expected JSON format:
    [
      {
        "prompt": "What laptops do you have?",
        "reference": "We have the ProBook Laptop 15...",
        "expected_tool_use": [{"tool_name": "...", "tool_input": {...}}]
      },
      ...
    ]
    """
    path = Path(dataset_path)
    if not path.exists():
        logger.error("Dataset not found: %s", dataset_path)
        sys.exit(1)

    with open(path) as f:
        cases = json.load(f)

    logger.info("Loaded %d eval cases from %s", len(cases), path.name)

    # Build DataFrame with prompt and session_inputs columns.
    # Use demo-user-001 which owns orders/invoices in the seeded Firestore data.
    session_inputs = types.evals.SessionInput(user_id="demo-user-001", state={})
    df = pd.DataFrame(cases)

    # Add session_inputs column (required for agent inference traces)
    df["session_inputs"] = [session_inputs] * len(df)

    return df


def run_inference(client: Client, agent_engine_id: str, dataset: pd.DataFrame):
    """Run inference against a deployed Agent Engine app.

    Args:
        client: Vertex AI Client.
        agent_engine_id: Full resource name of the Agent Engine app.
        dataset: DataFrame with 'prompt' and 'session_inputs' columns.

    Returns:
        EvaluationDataset with inference results.
    """
    logger.info("Running inference against: %s", agent_engine_id)
    logger.info("Sending %d prompts...", len(dataset))

    result_dataset = client.evals.run_inference(
        agent=agent_engine_id,
        src=dataset,
    )

    logger.info("Inference complete.")
    return result_dataset


def run_custom_inference(
    agent_engine_id: str,
    dataset: pd.DataFrame,
    delay: float = 3.0,
) -> types.EvaluationDataset:
    """Run inference by calling the Agent Engine directly via async_stream_query().

    This bypasses the eval SDK's inference parser which fails to extract
    final text responses from multi-agent systems using AgentTool.

    Key insight: ``stream_query()`` (sync) only yields the FIRST event
    (the function_call delegation) and stops. ``async_stream_query()``
    yields ALL events including the sub-agent execution and the root's
    final text response — matching the production backend's behavior.

    Args:
        agent_engine_id: Full resource name of the deployed Agent Engine app.
        dataset: DataFrame with 'prompt' column and optional 'reference'.
        delay: Seconds to wait between prompts (rate limit protection).

    Returns:
        EvaluationDataset with response and intermediate_events populated.
    """
    logger.info("Running custom inference against: %s", agent_engine_id)
    agent_engine = agent_engines.get(agent_engine_id)

    prompts = dataset["prompt"].tolist()
    refs = dataset.get("reference", pd.Series([""] * len(dataset))).tolist()

    requests = []
    responses = []
    intermediate_events_list = []
    references = []

    async def _query_single(prompt: str, max_retries: int = 3) -> tuple[str, list[dict]]:
        """Send one prompt via async_stream_query and extract response text.

        Retries on transient errors (503 UNAVAILABLE, connection resets).
        """
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                session = await agent_engine.async_create_session(user_id="demo-user-001", state={})
                session_id = session["id"]

                events = []
                response_text = ""

                async for event in agent_engine.async_stream_query(
                    user_id="demo-user-001",
                    session_id=session_id,
                    message=prompt,
                ):
                    events.append(event)

                    # Extract text from ALL events (same as production backend)
                    if isinstance(event, dict):
                        content = event.get("content", event.get("parts", {}))
                        if isinstance(content, dict):
                            for part in content.get("parts", []):
                                if isinstance(part, dict) and "text" in part:
                                    response_text += part["text"]

                        if "text" in event:
                            response_text += event["text"]

                # Build intermediate events matching the format the eval SDK produces
                # via run_inference(). Keep thought_signature and function_call.id —
                # these are required by the TOOL_USE_QUALITY rubric evaluator.
                # (Previously stripped for create_evaluation_run() which is broken anyway.)
                intermediate = []
                for evt in events[:-1] if len(events) > 1 else []:
                    if not isinstance(evt, dict) or "content" not in evt:
                        continue
                    content = evt["content"]
                    if isinstance(content, dict) and "parts" in content:
                        intermediate.append(
                            {
                                "event_id": evt.get("id", ""),
                                "content": content,
                            }
                        )

                return response_text, events, intermediate

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    backoff = 5 * (2 ** (attempt - 1))  # 5s, 10s
                    logger.warning(
                        "  Attempt %d/%d failed: %s — retrying in %ds",
                        attempt,
                        max_retries,
                        e,
                        backoff,
                    )
                    await asyncio.sleep(backoff)

        raise last_error

    async def _run_all():
        for i, prompt in enumerate(prompts):
            logger.info("[%d/%d] Sending: %s", i + 1, len(prompts), prompt[:80])

            try:
                response_text, events, intermediate = await _query_single(prompt)

                logger.info(
                    "  -> %d events, %d intermediate, %d chars: %s",
                    len(events),
                    len(intermediate),
                    len(response_text),
                    response_text[:100] + ("..." if len(response_text) > 100 else ""),
                )

                # Debug: dump raw events when no text was extracted
                if not response_text and events:
                    for j, evt in enumerate(events):
                        logger.warning(
                            "  [NO TEXT] Event %d keys: %s",
                            j,
                            list(evt.keys()) if isinstance(evt, dict) else type(evt),
                        )
                        logger.warning(
                            "  [NO TEXT] Event %d raw: %s",
                            j,
                            json.dumps(evt, default=str)[:800],
                        )

            except Exception as e:
                logger.error("  -> FAILED: %s", e)
                response_text = json.dumps({"error": str(e)})
                intermediate = []

            requests.append(prompt)
            responses.append(response_text or "(no response extracted)")
            intermediate_events_list.append(intermediate)
            references.append(refs[i])

            # Rate-limit delay between prompts
            if i < len(prompts) - 1 and delay > 0:
                await asyncio.sleep(delay)

    # Run the async inference loop
    asyncio.run(_run_all())

    logger.info(
        "Custom inference complete: %d/%d got responses",
        sum(1 for r in responses if not r.startswith("(no response") and not r.startswith('{"error"')),
        len(prompts),
    )

    # Build result DataFrame with the columns create_evaluation_run expects.
    # IMPORTANT: Must use "prompt" (not "request") — the eval service SDK
    # checks for _evals_constant.PROMPT == "prompt" when building
    # EvaluationItemRequest objects in _create_evaluation_set_from_dataframe().
    result_df = pd.DataFrame(
        {
            "prompt": requests,
            "response": responses,
            "reference": references,
            "intermediate_events": intermediate_events_list,
        }
    )

    return types.EvaluationDataset(eval_dataset_df=result_df)


def run_evaluation(
    client: Client,
    dataset_with_inference,
    agent_resource_name: str,
    config: dict,
    gcs_dest: str = None,
):
    """Run evaluation using Vertex AI Gen AI Evaluation Service (evaluate() API).

    Uses the synchronous evaluate() API which is reliable and accurate.
    Results are uploaded to GCS and logged to Vertex AI Experiments for
    console visibility (Vertex AI → Experiments → post-deploy-eval).

    Note: create_evaluation_run() (async pipeline API) was investigated
    exhaustively but consistently fails with "ProcessItemsResult: []" at the
    Vertex AI backend level — it is not used here.

    Args:
        client: Vertex AI Client.
        dataset_with_inference: EvaluationDataset from run_inference().
        agent_resource_name: Full resource name of the deployed Agent Engine app.
        config: Post-deploy eval config dict with 'metrics' key.
        gcs_dest: GCS path for eval results upload (optional).

    Returns:
        EvaluationResult with summary_metrics list.
    """
    metric_names = config.get("metrics", ["TOOL_USE_QUALITY", "FINAL_RESPONSE_QUALITY"])
    rubric_metrics = []

    metric_map = {
        "TOOL_USE_QUALITY": types.RubricMetric.TOOL_USE_QUALITY,
        "FINAL_RESPONSE_QUALITY": types.RubricMetric.FINAL_RESPONSE_QUALITY,
        "HALLUCINATION": types.RubricMetric.HALLUCINATION,
        "SAFETY": types.RubricMetric.SAFETY,
    }
    for name in metric_names:
        if name in metric_map:
            rubric_metrics.append(metric_map[name])
        else:
            logger.warning("Unknown metric: %s — skipping", name)

    logger.info("Evaluating with metrics: %s", metric_names)

    # -------------------------------------------------------------------------
    # Scoring: evaluate() — synchronous, reliable, always works.
    #
    # NOTE: create_evaluation_run() (async pipeline API) has been investigated
    # exhaustively but consistently fails with:
    #   "Cannot find ProcessItemsResult in pipeline data: []"
    # This is a Vertex AI backend issue — the pipeline's internal ProcessItems
    # step produces zero results regardless of data format, ADK field stripping,
    # GCS permissions, or agent_info configuration. All 20+ attempts failed.
    #
    # Console visibility is provided via log_to_vertex_experiments() which logs
    # metric scores to Vertex AI Experiments (visible at Vertex AI → Experiments).
    # -------------------------------------------------------------------------
    logger.info("Running evaluate() for scoring...")

    agent_name = "customer_support"
    agent_instruction = "You are a customer support coordinator. Route queries to the right " "specialist agent."
    try:
        from customer_support_mas.agents.root.agent import root_agent as _ra

        agent_name = _ra.name
        agent_instruction = _ra.instruction or agent_instruction
    except Exception as _e:
        logger.warning("Could not import root_agent: %s — using defaults", _e)

    # NOTE: agent_resource_name is intentionally omitted.
    # When provided, evaluate() re-runs its own SDK-based inference which
    # breaks on multi-agent AgentTool patterns and returns empty responses.
    # Without it, evaluate() uses our pre-computed responses and
    # intermediate_events from the custom async_stream_query() adapter.
    agent_info = types.evals.AgentInfo(
        name=agent_name,
        instruction=agent_instruction,
    )

    eval_config = types.EvaluateMethodConfig(dest=gcs_dest) if gcs_dest else None
    evaluation_result = client.evals.evaluate(
        dataset=dataset_with_inference,
        metrics=rubric_metrics,
        config=eval_config,
        agent_info=agent_info,
    )
    logger.info("evaluate() complete.")
    return evaluation_result


def log_to_vertex_experiments(
    evaluation_result,
    project_id: str,
    location: str,
    experiment_name: str = "post-deploy-eval",
    report_path: str = None,
    gcs_bucket: str = None,
    run_name: str = None,
):
    """Log evaluation results to Vertex AI Experiments for console visibility.

    Uploads the HTML report to GCS (if gcs_bucket is set) and logs the GCS URI
    as a parameter in the experiment run so the report is accessible from the
    Experiments console.

    Results appear at:
        Vertex AI → Experiments → post-deploy-eval

    Args:
        evaluation_result: EvaluationResult from evaluate().
        project_id: GCP project ID.
        location: GCP region.
        experiment_name: Experiment name (created if it doesn't exist).
        report_path: Local path to the HTML report file to upload.
        gcs_bucket: GCS bucket (gs://... or bare name) for HTML upload.
        run_name: Experiment run name — pass a pre-generated timestamp name so
            the HTML filename and the run name share the same timestamp.
    """
    try:
        from google.cloud import aiplatform

        aiplatform.init(project=project_id, location=location, experiment=experiment_name)

        if run_name is None:
            run_name = f"eval-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}"

        metrics = {}
        params = {}

        summary_list = getattr(evaluation_result, "summary_metrics", None)
        if isinstance(summary_list, list):
            for agg in summary_list:
                metric_name = getattr(agg, "metric_name", None) or ""
                mean_score = getattr(agg, "mean_score", None)
                if mean_score is not None:
                    short = metric_name.replace("_v1", "")
                    metrics[short] = float(mean_score)
                num_total = getattr(agg, "num_cases_total", None)
                num_error = getattr(agg, "num_cases_error", None)
                if num_total is not None:
                    params["num_cases_total"] = int(num_total)
                if num_error:
                    params["num_cases_error"] = int(num_error)

        if not metrics:
            logger.warning("No metrics to log to Vertex AI Experiments.")
            return

        # Upload HTML report to GCS and record the URI as a param
        if report_path and gcs_bucket and Path(report_path).exists():
            try:
                object_path = f"eval-reports/{run_name}.html"
                gcs_uri = upload_to_gcs(report_path, gcs_bucket, object_path)
                params["report_gcs_uri"] = gcs_uri
            except Exception as upload_err:
                logger.warning("Could not upload report to GCS: %s", upload_err)

        with aiplatform.start_run(run=run_name):
            aiplatform.log_params(params)
            aiplatform.log_metrics(metrics)

        logger.info(
            "Results logged to Vertex AI Experiments: experiment=%s run=%s",
            experiment_name,
            run_name,
        )
        logger.info(
            "View in console: https://console.cloud.google.com/vertex-ai/experiments?project=%s",
            project_id,
        )
    except Exception as e:
        logger.warning("Failed to log to Vertex AI Experiments: %s", e)


def upload_to_gcs(local_path: str, gcs_bucket: str, object_path: str) -> str:
    """Upload a local file to GCS and return the gs:// URI."""
    from google.cloud import storage

    bucket_name = gcs_bucket.lstrip("gs://").split("/")[0]
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(object_path)
    blob.upload_from_filename(local_path)
    gcs_uri = f"gs://{bucket_name}/{object_path}"
    logger.info("Uploaded to GCS: %s", gcs_uri)
    return gcs_uri


def _gcs_bucket_and_path(gcs_uri: str) -> tuple[str, str]:
    """Parse gs://bucket/path into (bucket_name, object_path)."""
    stripped = gcs_uri.lstrip("gs://")
    parts = stripped.split("/", 1)
    bucket = parts[0]
    path = parts[1] if len(parts) > 1 else ""
    return bucket, path


def load_baseline_from_gcs(gcs_uri: str) -> dict | None:
    """Load baseline scores JSON from GCS. Returns None if not found."""
    try:
        from google.cloud import storage

        bucket_name, object_path = _gcs_bucket_and_path(gcs_uri)
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(object_path)
        if not blob.exists():
            logger.info("No baseline found at %s (first run)", gcs_uri)
            return None
        data = json.loads(blob.download_as_text())
        logger.info("Loaded baseline from %s", gcs_uri)
        return data
    except Exception as e:
        logger.warning("Could not load baseline from GCS: %s", e)
        return None


def save_baseline_to_gcs(gcs_uri: str, details: dict, run_name: str):
    """Save current eval scores and composite score to GCS as the new baseline."""
    try:
        from google.cloud import storage

        bucket_name, object_path = _gcs_bucket_and_path(gcs_uri)
        scores = {k: v for k, v in details.items() if not k.startswith("_")}
        payload = {
            "scores": scores,
            "composite_score": compute_composite_score(scores),
            "metadata": {"run_name": run_name, "saved_at": pd.Timestamp.now().isoformat()},
        }
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(object_path)
        blob.upload_from_string(json.dumps(payload, indent=2, default=str), content_type="application/json")
        logger.info("Baseline saved to GCS: %s", gcs_uri)
    except Exception as e:
        logger.warning("Could not save baseline to GCS: %s", e)


# Composite score weights for promotion/regression decisions.
# Reflects business priority: tool use + response quality = 80% of what matters.
# Safety/hallucination are guardrails (Model Armor handles most safety) = 20%.
# Per-metric absolute thresholds remain as a hard floor (separate check).
COMPOSITE_WEIGHTS = {
    "Tool Use Quality": 0.40,
    "Final Response Quality": 0.40,
    "Hallucination": 0.10,
    "Safety": 0.10,
}


def compute_composite_score(details: dict) -> float:
    """Compute weighted composite score from per-metric details.

    Absorbs LLM judge variance across metrics — one metric dropping slightly
    is offset by others improving, preventing false regression signals.
    Only metrics present in both COMPOSITE_WEIGHTS and details are included;
    weights are renormalized if some metrics are missing (e.g. fast profile).
    """
    weighted_sum = 0.0
    total_weight = 0.0
    for metric, weight in COMPOSITE_WEIGHTS.items():
        if metric in details:
            weighted_sum += details[metric]["score"] * weight
            total_weight += weight
    return weighted_sum / total_weight if total_weight > 0 else 0.0


def compare_to_baseline(details: dict, baseline: dict, threshold: float) -> tuple[bool, list[str]]:
    """Compare current composite score against baseline composite score.

    Uses a weighted composite score instead of per-metric comparison to absorb
    LLM judge variance — a single case being judged differently across runs
    (1/9 = 11% per-metric swing) no longer triggers a false regression signal.

    A regression is flagged when:
        (baseline_composite - current_composite) / baseline_composite > threshold

    Per-metric absolute thresholds remain as a hard floor (checked separately
    in check_thresholds). This function only governs the promote/rollback decision.
    """
    baseline_scores = baseline.get("scores", {})
    baseline_composite = baseline.get("composite_score")

    current_composite = compute_composite_score(details)

    if baseline_composite is None:
        # Old baseline format — recompute from per-metric scores
        baseline_composite = compute_composite_score(baseline_scores)

    if baseline_composite <= 0:
        return True, []

    relative_drop = (baseline_composite - current_composite) / baseline_composite
    if relative_drop > threshold:
        return False, [
            f"Composite score: {baseline_composite:.4f} -> {current_composite:.4f} "
            f"(relative drop {relative_drop:.1%} > threshold {threshold:.1%})"
        ]
    return True, []


def save_html_report(evaluation_result, output_path: str):
    """Save the full HTML evaluation report to a local file.

    evaluation_result.show() skips HTML generation when not in a Jupyter
    environment. This patches the IPython env check to force HTML generation,
    intercepts the display call, and saves the HTML to a file.
    """
    try:
        import IPython.display as ipython_display
        import vertexai._genai._evals_visualization as _viz

        captured = []

        # Patch the IPython env check so HTML generation runs in terminal
        _original_is_ipython = _viz._is_ipython_env
        _viz._is_ipython_env = lambda: True

        # Intercept the display call to capture the HTML bytes
        _original_display = ipython_display.display

        def _capture(*args, **kwargs):
            for obj in args:
                data = getattr(obj, "data", None)
                if isinstance(data, str):
                    captured.append(data)

        ipython_display.display = _capture
        try:
            evaluation_result.show()
        finally:
            _viz._is_ipython_env = _original_is_ipython
            ipython_display.display = _original_display

        if captured:
            Path(output_path).write_text(captured[0])
            logger.info("Full eval report saved to: %s", output_path)
            logger.info("Open in browser: file://%s", Path(output_path).resolve())
        else:
            logger.warning("Could not capture HTML from show()")
    except ImportError:
        logger.warning("IPython not installed — cannot save HTML report")
    except Exception as e:
        logger.warning("Failed to save HTML report: %s", e)


def _resolve_threshold(metric_name: str, thresholds: dict) -> float:
    """Match a Vertex AI metric name to a configured threshold.

    Vertex AI returns metrics like:
        customer_support/tool_use_quality_v1/AVERAGE
    Our config uses short names like:
        TOOL_USE_QUALITY

    We match by checking if the short name (lowercased) appears in
    the full metric name (lowercased).
    """
    metric_lower = metric_name.lower()
    for config_key, threshold_val in thresholds.items():
        # e.g. "TOOL_USE_QUALITY" -> "tool_use_quality"
        if config_key.lower() in metric_lower:
            return threshold_val
    return 0.0


def check_thresholds(result, config: dict) -> tuple[bool, dict]:
    """Check evaluation results against thresholds.

    Args:
        result: EvaluationResult from evaluate().
              summary_metrics is list[AggregatedMetricResult] with
              .metric_name and .mean_score attributes.
        config: Config dict with 'thresholds' key.

    Returns:
        (passed: bool, details: dict with per-metric results)
    """
    thresholds = config.get("thresholds", {})
    details = {}
    all_passed = True
    total_cases = None
    error_cases = 0

    summary_list = getattr(result, "summary_metrics", None)
    if isinstance(summary_list, list):
        for agg in summary_list:
            metric_name = getattr(agg, "metric_name", None) or ""
            mean_score = getattr(agg, "mean_score", None)
            num_total = getattr(agg, "num_cases_total", None)
            num_error = getattr(agg, "num_cases_error", None) or 0

            if total_cases is None and num_total is not None:
                total_cases = num_total
            error_cases = max(error_cases, num_error)

            if mean_score is None:
                continue

            score = float(mean_score)
            threshold = _resolve_threshold(metric_name, thresholds)
            passed = score >= threshold
            display_name = metric_name.replace("_v1", "").replace("_", " ").title()
            details[display_name] = {"score": round(score, 4), "threshold": threshold, "passed": passed}
            if not passed:
                all_passed = False

    if total_cases is not None:
        details["_total_items"] = total_cases
    if error_cases:
        details["_failed_items"] = error_cases

    return all_passed, details


def print_results(passed: bool, details: dict):
    """Print evaluation results table."""
    print("\n" + "=" * 70)
    print("POST-DEPLOY EVALUATION RESULTS")
    print("=" * 70)

    # Extract and display item counts
    total_items = details.pop("_total_items", None)
    failed_items = details.pop("_failed_items", None)

    if total_items is not None:
        errors = failed_items or 0
        succeeded = total_items - errors
        print(f"  Items: {total_items} total, {succeeded} succeeded, {errors} errors")
        print()

    if not details:
        print("  No metric results available.")
        print("=" * 70)
        return

    # Header
    print(f"{'Metric':<40} {'Score':>8} {'Threshold':>10} {'Status':>8}")
    print("-" * 70)

    for metric, info in details.items():
        status = "PASS" if info["passed"] else "FAIL"
        status_icon = "+" if info["passed"] else "X"
        print(f"  [{status_icon}] {metric:<36} {info['score']:>8.4f} " f"{info['threshold']:>10.2f} {status:>8}")

    print("-" * 70)
    composite = compute_composite_score(details)
    print(f"  Composite score: {composite:.4f}  (weights: tool_use=40%, response=40%, hallucination=10%, safety=10%)")
    overall = "PASSED" if passed else "FAILED"
    print(f"  Overall: {overall}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Post-deployment agent evaluation using Vertex AI Gen AI Eval Service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--agent-engine-id",
        required=True,
        help="Full resource name of the deployed Agent Engine app "
        "(e.g., projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/ENGINE_ID)",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        choices=["standard", "full"],
        help="Eval config profile (default: standard)",
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Path to eval dataset JSON (default: {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--output",
        help="Save results to JSON file",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Seconds between prompts during inference (default: 3.0)",
    )
    parser.add_argument(
        "--custom-inference",
        action="store_true",
        help="Use custom async_stream_query() adapter instead of SDK run_inference(). "
        "Needed for multi-agent setups where stream_query() only returns the first event.",
    )
    # Legacy alias kept for backwards-compatibility
    parser.add_argument("--sdk-inference", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--save-inference",
        help="Save raw inference results (prompts + responses) to JSON for debugging",
    )
    parser.add_argument(
        "--inspect-sdk-events",
        metavar="FILE",
        help="Run SDK inference, dump the raw intermediate_events to FILE (JSON), then exit. "
        "Use this to inspect the exact format the SDK produces for option-2 format matching.",
    )
    parser.add_argument(
        "--report",
        default="eval_report.html",
        help="Save full HTML evaluation report to this file (default: eval_report.html). "
        "Contains per-item scores, explanations, and agent traces — open in a browser.",
    )
    parser.add_argument(
        "--update-baseline",
        metavar="GCS_URI",
        help="GCS path to a baseline scores JSON (gs://bucket/path.json). "
        "Compares current scores against the stored baseline. "
        "If no baseline exists, saves current scores as the first baseline. "
        "If no regression detected, updates the baseline. "
        "If regression > --regression-threshold, exits 1.",
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=0.05,
        help="Maximum allowed relative score drop vs baseline before flagging a regression "
        "(default: 0.05 = 5%%). A score drop of >5%% relative to the baseline triggers a "
        "regression. Example: baseline=0.80, threshold=0.05 flags if current < 0.76.",
    )

    args = parser.parse_args()

    # Initialize
    project_id, location = init_vertex_ai()

    # Shared timestamp — used for the run name, report filename, and GCS path
    # so they all line up in the console and bucket.
    run_timestamp = pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
    run_name = f"eval-{run_timestamp}"

    # Default report filename matches the Vertex AI Experiments run name exactly:
    # eval-20260306-152928.html → same name as the experiment run → easy to correlate
    if args.report == "eval_report.html":
        args.report = f"{run_name}.html"

    # GCS destination for eval results (optional — results are returned in-memory if not set)
    gcs_bucket = os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET", "")
    gcs_dest = f"{gcs_bucket}/eval-results" if gcs_bucket else None
    if not gcs_dest:
        logger.info("GOOGLE_CLOUD_STORAGE_BUCKET not set — eval results will not be uploaded to GCS")

    # Create client with v1beta1 API (required for evals)
    client = Client(
        project=project_id,
        location=location,
        http_options=genai_types.HttpOptions(api_version="v1beta1"),
    )

    # Load config and dataset
    config = load_post_deploy_config(profile=args.profile)
    dataset = load_dataset(args.dataset)

    # Resolve full resource name for the Agent Engine.
    # agent_engines.get() accepts short numeric IDs but AgentInfo requires
    # the full "projects/.../locations/.../reasoningEngines/..." resource name.
    # Prefer the AGENT_ENGINE_RESOURCE_NAME env var (set in .env) over the
    # short numeric ID passed via --agent-engine-id.
    agent_engine_id = args.agent_engine_id
    env_resource_name = os.getenv("AGENT_ENGINE_RESOURCE_NAME", "")
    if env_resource_name and env_resource_name.startswith("projects/"):
        agent_engine_id = env_resource_name
        logger.info("Using AGENT_ENGINE_RESOURCE_NAME from .env: %s", agent_engine_id)
    agent_resource_name = agent_engine_id  # fallback
    try:
        ae_obj = agent_engines.get(agent_engine_id)
        resolved = getattr(ae_obj, "resource_name", None) or getattr(ae_obj, "name", None)
        if resolved and resolved.startswith("projects/"):
            agent_resource_name = resolved
            logger.info("Resolved full resource name: %s", agent_resource_name)
        else:
            logger.warning(
                "Could not resolve full resource name from agent object " "(attr: %s) — using provided ID", resolved
            )
    except Exception as e:
        logger.warning("Could not resolve agent resource name: %s — using provided ID", e)

    logger.info("Profile: %s", args.profile)
    logger.info("Metrics: %s", config.get("metrics", []))
    logger.info("GCS dest: %s", gcs_dest)

    # --inspect-sdk-events: dump SDK intermediate_events format and exit (option-2 research tool)
    if getattr(args, "inspect_sdk_events", None):
        logger.info("--inspect-sdk-events: running SDK inference and dumping intermediate_events...")
        sdk_result = run_inference(client, agent_engine_id, dataset)
        sdk_df = getattr(sdk_result, "eval_dataset_df", None)
        if sdk_df is None or "intermediate_events" not in sdk_df.columns:
            logger.error("SDK inference did not produce intermediate_events column")
            sys.exit(1)
        records = []
        for i, (_, row) in enumerate(sdk_df.iterrows()):
            records.append(
                {
                    "case": i,
                    "prompt": row.get("prompt", ""),
                    "response": row.get("response", ""),
                    "intermediate_events": row["intermediate_events"],
                }
            )
        with open(args.inspect_sdk_events, "w") as f:
            json.dump(records, f, indent=2, default=str)
        logger.info("SDK intermediate_events dumped to %s — inspect to implement option 2", args.inspect_sdk_events)
        sys.exit(0)

    # Step 1: Run inference against deployed agent
    # Per official docs: use SDK's run_inference() as the default — it creates eval
    # items in the exact format the pipeline expects (no ADK-specific fields).
    # Use --custom-inference only if SDK inference fails for your multi-agent setup.
    print("\n--- Step 1/3: Running inference against deployed agent ---")
    use_custom = args.custom_inference or args.sdk_inference  # sdk_inference is legacy alias
    if not use_custom:
        logger.info("Using SDK's built-in run_inference() (recommended per docs)")
        dataset_with_inference = run_inference(client, agent_engine_id, dataset)
    else:
        logger.info("Using custom async_stream_query() adapter")
        dataset_with_inference = run_custom_inference(
            agent_engine_id,
            dataset,
            delay=args.delay,
        )

    # Save inference results for debugging if requested
    if args.save_inference:
        inf_df = dataset_with_inference.eval_dataset_df if hasattr(dataset_with_inference, "eval_dataset_df") else None
        if inf_df is not None:
            # Convert to serializable format — include intermediate_events for debugging
            inf_records = []
            for _, row in inf_df.iterrows():
                intermediate = row.get("intermediate_events", [])
                inf_records.append(
                    {
                        "prompt": row.get("prompt", ""),
                        "response": row.get("response", ""),
                        "reference": row.get("reference", ""),
                        "intermediate_events_count": len(intermediate) if isinstance(intermediate, list) else 0,
                        "intermediate_events": intermediate,
                    }
                )
            with open(args.save_inference, "w") as f:
                json.dump(inf_records, f, indent=2, default=str)
            logger.info("Inference results saved to %s", args.save_inference)

    # Step 2: Run evaluation (evaluate() API + Vertex AI Experiments logging)
    print("\n--- Step 2/3: Evaluating responses ---")
    evaluation_result = run_evaluation(
        client,
        dataset_with_inference,
        agent_resource_name,
        config,
        gcs_dest=gcs_dest,
    )

    # Save full HTML report (per-item scores, explanations, traces)
    save_html_report(evaluation_result, args.report)

    # Log to Vertex AI Experiments — uploads HTML to GCS and records the URI as a param
    log_to_vertex_experiments(
        evaluation_result,
        project_id,
        location,
        report_path=args.report,
        gcs_bucket=gcs_bucket,
        run_name=run_name,
    )

    # Step 3: Check thresholds and report
    print("\n--- Step 3/3: Checking thresholds ---")
    passed, details = check_thresholds(evaluation_result, config)
    print_results(passed, details)

    # Baseline comparison (nightly regression monitoring)
    if args.update_baseline:
        baseline = load_baseline_from_gcs(args.update_baseline)
        if baseline is None:
            logger.info("First run — saving current scores as baseline at %s", args.update_baseline)
            save_baseline_to_gcs(args.update_baseline, details, run_name)
        else:
            regression_passed, regressions = compare_to_baseline(details, baseline, args.regression_threshold)
            if regression_passed:
                logger.info("No regression detected vs baseline (threshold: %.1f%%)", args.regression_threshold * 100)
                save_baseline_to_gcs(args.update_baseline, details, run_name)
            else:
                logger.error("REGRESSION DETECTED (threshold: %.1f%%):", args.regression_threshold * 100)
                for r in regressions:
                    logger.error("  %s", r)
                passed = False  # regression overrides threshold pass

    # Save results if requested
    if args.output:
        summary_metrics_out = {}
        for agg in getattr(evaluation_result, "summary_metrics", None) or []:
            if not hasattr(agg, "metric_name"):
                continue
            n = getattr(agg, "metric_name", None) or ""
            summary_metrics_out[n] = {
                "mean_score": getattr(agg, "mean_score", None),
                "num_cases_total": getattr(agg, "num_cases_total", None),
                "num_cases_valid": getattr(agg, "num_cases_valid", None),
                "num_cases_error": getattr(agg, "num_cases_error", None),
            }

        results = {
            "config": {
                "profile": args.profile,
                "agent_engine_id": args.agent_engine_id,
                "dataset": args.dataset,
            },
            "threshold_check": {
                "passed": passed,
                "details": details,
            },
            "summary_metrics": summary_metrics_out,
        }

        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)

        logger.info("Results saved to %s", args.output)

    # Exit code based on threshold check
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
