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

import argparse
import logging
import os
import sys

import backoff
from data_ingestion_pipeline.pipeline import pipeline
from kfp import compiler

PIPELINE_FILE_NAME = "data_processing_pipeline.json"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for pipeline configuration."""

    parser = argparse.ArgumentParser(description="Pipeline configuration")
    parser.add_argument(
        "--project", default=os.getenv("PROJECT_ID"), help="GCP Project ID"
    )
    parser.add_argument(
        "--region",
        default=os.getenv("REGION"),
        help="Vertex AI Pipelines region",
    )
    parser.add_argument(
        "--vector-search-location",
        default=os.getenv("VECTOR_SEARCH_LOCATION"),
        help="Vector Search 2.0 location (defaults to REGION if not set)",
    )
    parser.add_argument(
        "--collection-id",
        default=os.getenv("VECTOR_SEARCH_COLLECTION_ID"),
        help="Vector Search 2.0 Collection ID",
    )
    parser.add_argument(
        "--service-account",
        default=os.getenv("SERVICE_ACCOUNT"),
        help="Service account",
    )
    parser.add_argument(
        "--pipeline-root",
        default=os.getenv("PIPELINE_ROOT"),
        help="Pipeline root directory",
    )
    parser.add_argument(
        "--pipeline-name",
        default=os.getenv("PIPELINE_NAME"),
        help="Pipeline name",
    )
    parser.add_argument(
        "--disable-caching",
        type=bool,
        default=os.getenv("DISABLE_CACHING", "false").lower() == "true",
        help="Enable pipeline caching",
    )
    parser.add_argument(
        "--cron-schedule",
        default=os.getenv("CRON_SCHEDULE", None),
        help="Cron schedule",
    )
    parser.add_argument(
        "--schedule-only",
        type=bool,
        default=os.getenv("SCHEDULE_ONLY", "false").lower() == "true",
        help="Schedule only (do not submit)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run locally using KFP SubprocessRunner instead of "
        "submitting to Vertex AI",
    )
    parsed_args = parser.parse_args()

    # Fall back to region if vector_search_location not set
    if not parsed_args.vector_search_location:
        parsed_args.vector_search_location = parsed_args.region

    # In local mode, only project_id and region are strictly required
    if parsed_args.local:
        missing_params = []
        required_params = {
            "project_id": parsed_args.project,
            "region": parsed_args.region,
            "collection_id": parsed_args.collection_id,
        }

        for param_name, param_value in required_params.items():
            if param_value is None:
                missing_params.append(param_name)

        if missing_params:
            logging.error(
                "Error: The following required parameters are missing:"
            )
            for param in missing_params:
                logging.error(f"  - {param}")
            sys.exit(1)

        return parsed_args

    # Validate required parameters for remote execution
    missing_params = []
    required_params = {
        "project_id": parsed_args.project,
        "region": parsed_args.region,
        "service_account": parsed_args.service_account,
        "pipeline_root": parsed_args.pipeline_root,
        "pipeline_name": parsed_args.pipeline_name,
        "collection_id": parsed_args.collection_id,
    }

    for param_name, param_value in required_params.items():
        if param_value is None:
            missing_params.append(param_name)

    if missing_params:
        logging.error("Error: The following required parameters are missing:")
        for param in missing_params:
            logging.error(f"  - {param}")
        logging.error(
            "\nPlease provide these parameters either through "
            "environment variables or command line arguments."
        )
        sys.exit(1)

    return parsed_args


def run_local(args: argparse.Namespace) -> None:
    """Run the pipeline locally using KFP SubprocessRunner.

    This executes the pipeline components as local subprocesses
    without needing Vertex AI Pipelines infrastructure.
    """
    from kfp import local

    local.init(runner=local.SubprocessRunner(use_venv=False))

    from datetime import UTC, datetime

    logging.info("Running pipeline locally...")
    pipeline(
        project_id=args.project,
        location=args.vector_search_location,
        schedule_time=datetime.now(UTC).isoformat(),
        collection_id=args.collection_id,
    )
    logging.info("Local pipeline run completed!")


@backoff.on_exception(
    backoff.expo,
    Exception,
    max_tries=3,
    max_time=3600,
    on_backoff=lambda details: logging.warning(
        f"Pipeline attempt {details['tries']} failed, "
        f"retrying in {details['wait']:.1f}s..."
    ),
)
def submit_and_wait_pipeline(
    pipeline_job_params: dict, service_account: str
) -> None:
    """Submit pipeline job and wait for completion with retry logic."""
    from google.cloud import aiplatform

    job = aiplatform.PipelineJob(**pipeline_job_params)
    job.submit(service_account=service_account)
    job.wait()


if __name__ == "__main__":
    args = parse_args()

    if args.local:
        run_local(args)
        sys.exit(0)

    if args.schedule_only and not args.cron_schedule:
        logging.error("Missing --cron-schedule argument for scheduling")
        sys.exit(1)

    # Print configuration
    logging.info("\nConfiguration:")
    logging.info("--------------")
    # Print all arguments dynamically
    for arg_name, arg_value in vars(args).items():
        logging.info(f"{arg_name}: {arg_value}")
    logging.info("--------------\n")

    from kfp import dsl

    compiler.Compiler().compile(
        pipeline_func=pipeline, package_path=PIPELINE_FILE_NAME
    )
    # Create common pipeline job parameters
    pipeline_job_params = {
        "display_name": args.pipeline_name,
        "template_path": PIPELINE_FILE_NAME,
        "pipeline_root": args.pipeline_root,
        "project": args.project,
        "enable_caching": (not args.disable_caching),
        "location": args.region,
        "parameter_values": {
            "project_id": args.project,
            "location": args.vector_search_location,
            "schedule_time": dsl.PIPELINE_JOB_SCHEDULE_TIME_UTC_PLACEHOLDER,
            "collection_id": args.collection_id,
        },
    }

    if not args.schedule_only:
        logging.info("Running pipeline and waiting for completion...")
        submit_and_wait_pipeline(pipeline_job_params, args.service_account)
        logging.info("Pipeline completed!")

    if args.cron_schedule and args.schedule_only:
        from google.cloud import aiplatform

        # Create pipeline job instance for scheduling
        job = aiplatform.PipelineJob(**pipeline_job_params)
        pipeline_job_schedule = aiplatform.PipelineJobSchedule(
            pipeline_job=job,
            display_name=f"{args.pipeline_name} Weekly Ingestion Job",
        )

        schedule_list = pipeline_job_schedule.list(
            filter=f'display_name="{args.pipeline_name} Weekly Ingestion Job"',
            project=args.project,
            location=args.region,
        )
        logging.info("Schedule lists found: %s", schedule_list)
        if not schedule_list:
            pipeline_job_schedule.create(
                cron=args.cron_schedule, service_account=args.service_account
            )
            logging.info("Schedule created")
        else:
            schedule_list[0].update(cron=args.cron_schedule)
            logging.info("Schedule updated")

    # Clean up pipeline file
    if os.path.exists(PIPELINE_FILE_NAME):
        os.remove(PIPELINE_FILE_NAME)
        logging.info(f"Deleted {PIPELINE_FILE_NAME}")
