# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Submit the RAG ingestion pipeline to Vertex AI Pipelines.

This is the main entry point for running the pipeline. It handles:
  1. Loading configuration from .env (via python-dotenv)
  2. Parsing CLI arguments (all default to .env values)
  3. Compiling the KFP pipeline DAG into a JSON spec
  4. Submitting to Vertex AI Pipelines
  5. Optionally creating a recurring schedule

Usage:
    make data-ingestion-remote
    # or: PYTHONPATH=.:data_ingestion_pipeline uv run python \
    #       data_ingestion_pipeline/data_ingestion_pipeline/submit_pipeline.py \
    #       --service-account=SA_EMAIL \
    #       --pipeline-root=gs://bucket/pipeline-root
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import backoff
from kfp import compiler

from data_ingestion_pipeline.pipeline import pipeline

# Add project root to sys.path so that `import pipeline` and
# `import utils` resolve to the shared packages at the repo root.
# This is needed when running from the data_ingestion_pipeline/ subdirectory.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Compiled pipeline spec is written to this temp file, submitted to
# Vertex AI, then deleted after submission.
PIPELINE_FILE_NAME = "rag_ingestion_pipeline.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Every pipeline parameter defaults to its .env value via
    os.getenv(). Remote runs only need --service-account and
    --pipeline-root; any parameter can be overridden from the CLI for
    one-off runs.
    """
    parser = argparse.ArgumentParser(description="RAG ingestion pipeline")

    # GCP config — from .env
    parser.add_argument(
        "--project-id",
        default=os.getenv("GOOGLE_CLOUD_PROJECT"),
        help="GCP project ID",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("GOOGLE_CLOUD_LOCATION"),
        help="GCP region",
    )

    # GCS config — from .env
    parser.add_argument(
        "--gcs-prefix",
        default=os.getenv("GCS_PREFIX", "documents/"),
        help="GCS prefix",
    )

    # BQ config — from .env
    parser.add_argument(
        "--bq-dataset",
        default=os.getenv("BQ_DATASET", "rag_pipeline"),
        help="BQ dataset",
    )

    # VS config — from .env
    parser.add_argument(
        "--vs-collection-id",
        default=os.getenv(
            "VS_COLLECTION_ID", "multiformat-hybrid-rag-collection"
        ),
        help="VS2 chunks collection ID",
    )
    parser.add_argument(
        "--vs-documents-collection-id",
        default=os.getenv(
            "VS_DOCUMENTS_COLLECTION_ID", "multiformat-hybrid-rag-documents"
        ),
        help="VS2 documents-by-file_id collection ID",
    )

    # Chunking config — from .env
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(os.getenv("CHUNK_SIZE", "800")),
        help="Chunk size in characters",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=int(os.getenv("CHUNK_OVERLAP", "50")),
        help="Chunk overlap in characters",
    )
    parser.add_argument(
        "--vs-batch-size",
        type=int,
        default=int(os.getenv("VS_BATCH_SIZE", "250")),
        help="VS2 batch size (max 250)",
    )

    # Pipeline behavior
    parser.add_argument(
        "--rechunk-all",
        action="store_true",
        help="Force re-chunk all files",
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Skip the cleanup step",
    )

    # Remote execution params
    parser.add_argument(
        "--service-account",
        default=os.getenv("SERVICE_ACCOUNT"),
        help="Service account for Vertex AI Pipelines",
    )
    parser.add_argument(
        "--pipeline-root",
        default=os.getenv("PIPELINE_ROOT"),
        help="GCS root for pipeline artifacts",
    )
    parser.add_argument(
        "--pipeline-name",
        default=os.getenv("PIPELINE_NAME", "rag-ingestion"),
        help="Pipeline display name",
    )
    parser.add_argument(
        "--disable-caching",
        action="store_true",
        help="Disable Vertex AI pipeline caching",
    )
    parser.add_argument(
        "--cron-schedule",
        default=os.getenv("CRON_SCHEDULE"),
        help="Cron schedule for recurring runs",
    )
    parser.add_argument(
        "--schedule-only",
        action="store_true",
        help="Only create/update schedule, don't run now",
    )

    args = parser.parse_args()

    # Validate required params
    required = {
        "project_id": args.project_id,
        "region": args.region,
        "service_account": args.service_account,
        "pipeline_root": args.pipeline_root,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error("Missing required parameters: %s", ", ".join(missing))
        logger.error("Set them in .env or pass as CLI arguments.")
        sys.exit(1)

    return args


def get_pipeline_params(args: argparse.Namespace) -> dict:
    """Build pipeline parameter dict from parsed args."""
    return {
        "project_id": args.project_id,
        "region": args.region,
        "gcs_prefix": args.gcs_prefix,
        "bq_dataset": args.bq_dataset,
        "vs_collection_id": args.vs_collection_id,
        "vs_documents_collection_id": args.vs_documents_collection_id,
        "chunk_size": args.chunk_size,
        "chunk_overlap": args.chunk_overlap,
        "vs_batch_size": args.vs_batch_size,
        "rechunk_all": args.rechunk_all,
        "skip_cleanup": args.skip_cleanup,
    }


@backoff.on_exception(
    backoff.expo,  # Exponential backoff: 1s, 2s, 4s, ...
    Exception,  # Retry on any exception (transient API errors, etc.)
    max_tries=3,  # Give up after 3 attempts
    max_time=3600,  # Hard ceiling: 1 hour total (including wait time)
    on_backoff=lambda details: logging.warning(
        f"Pipeline attempt {details['tries']} failed, retrying in {details['wait']:.1f}s..."
    ),
)
def submit_and_wait(pipeline_job_params: dict, service_account: str) -> None:
    """Submit pipeline job to Vertex AI and block until it completes.

    Creates a PipelineJob from the compiled JSON spec, submits it to
    Vertex AI Pipelines, and polls until the DAG finishes (success or
    failure). The service account is the identity under which the
    pipeline containers run — it needs BQ, GCS, VS2, and Cloud Functions
    permissions (provisioned via Terraform).

    Raises on pipeline failure after all retries are exhausted.
    """
    from google.cloud import aiplatform

    job = aiplatform.PipelineJob(**pipeline_job_params)
    # submit() uploads the pipeline spec and starts execution.
    # The SDK prints the pipeline console URL to stdout.
    job.submit(service_account=service_account)
    # wait() blocks and polls the job status until terminal state.
    job.wait()


if __name__ == "__main__":
    # ── Load .env ─────────────────────────────────────────────────
    # Resolve .env relative to this file (two parents up = project root).
    # override=False means CLI env vars take precedence over .env,
    # and python-dotenv handles ${VAR} interpolation within the file
    # (e.g. GCS_BUCKET=${GOOGLE_CLOUD_PROJECT}-rag-docs resolves correctly).
    config_env = Path(__file__).parent.parent.parent / ".env"
    if config_env.exists():
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=config_env, override=False)

    args = parse_args()

    if args.schedule_only and not args.cron_schedule:
        logger.error("Missing --cron-schedule for scheduling")
        sys.exit(1)

    # Log all resolved configuration for debugging
    logger.info("Configuration:")
    for k, v in vars(args).items():
        logger.info("  %s: %s", k, v)

    # ── Compile pipeline ────────────────────────────────────────────────
    # KFP compiler converts the @dsl.pipeline function into a JSON spec
    # (Argo Workflow format) that Vertex AI Pipelines can execute.
    # The spec includes container images, parameters, and DAG structure.
    compiler.Compiler().compile(
        pipeline_func=pipeline, package_path=PIPELINE_FILE_NAME
    )

    # Build the PipelineJob configuration for the Vertex AI SDK.
    # pipeline_root is the GCS path where Vertex AI stores intermediate
    # artifacts (component outputs, executor logs, etc.).
    pipeline_job_params = {
        "display_name": args.pipeline_name,
        "template_path": PIPELINE_FILE_NAME,
        "pipeline_root": args.pipeline_root,
        "project": args.project_id,
        "enable_caching": not args.disable_caching,
        "location": args.region,
        "parameter_values": get_pipeline_params(args),
    }

    # ── Submit and wait ─────────────────────────────────────────────────
    # Uploads the compiled spec to Vertex AI, starts execution, and
    # blocks until the pipeline reaches a terminal state. The pipeline
    # URL is printed by the SDK during submit().
    if not args.schedule_only:
        logger.info("Submitting pipeline and waiting for completion...")
        submit_and_wait(pipeline_job_params, args.service_account)
        logger.info("Pipeline completed!")

    # ── Schedule (optional) ─────────────────────────────────────────────
    # Creates or updates a recurring schedule for the pipeline whenever
    # --cron-schedule is passed. Combine with --schedule-only to skip the
    # immediate run and only manage the schedule.
    # Example: --cron-schedule="0 2 * * *"
    if args.cron_schedule:
        from google.cloud import aiplatform

        job = aiplatform.PipelineJob(**pipeline_job_params)
        schedule = aiplatform.PipelineJobSchedule(
            pipeline_job=job,
            display_name=f"{args.pipeline_name} Scheduled Ingestion",
        )

        # Check for existing schedule to update rather than duplicate
        existing = schedule.list(
            filter=f'display_name="{args.pipeline_name} Scheduled Ingestion"',
            project=args.project_id,
            location=args.region,
        )
        if not existing:
            schedule.create(
                cron=args.cron_schedule, service_account=args.service_account
            )
            logger.info("Schedule created")
        else:
            existing[0].update(cron=args.cron_schedule)
            logger.info("Schedule updated")

    # Clean up the compiled JSON spec — it's a temp artifact only needed
    # for submission and shouldn't be committed to the repo.
    if os.path.exists(PIPELINE_FILE_NAME):
        os.remove(PIPELINE_FILE_NAME)
