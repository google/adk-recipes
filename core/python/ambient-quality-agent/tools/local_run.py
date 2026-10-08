# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run the AQA investigation workflow locally for quick manual checks.

This mirrors how Agent Runtime drives the deployed workflow, but uses
ADK's `InMemoryRunner` so you can exercise the pipeline end-to-end on
your machine without deploying.

The AQA `config` module reads its inputs from the environment at import
time (see `ambient_quality_agent.config`). This script sets sensible
local defaults for the required vars before importing the agent, so a
bare `uv run python tools/local_run.py` works out of the box. Override
any of them by exporting the matching env var first, for example:

    AQA_OBSERVED_AGENT_NAME=my-agent GOOGLE_CLOUD_PROJECT=my-project \
        uv run python tools/local_run.py

This drives the ``investigation_workflow`` graph directly (the same graph
the deployed agent runs in its durable background job via
``core.investigation.job_execution.run_investigation_graph``), rather than
the conversational orchestrator that sits in front of it. That keeps local
runs focused on the pipeline and avoids needing a chat LLM backend.

The window and budget are derived up front (via
``derive_window_and_budget``, matching orchestrator submission)
and seeded into state. The graph then runs the per-scope ``eval`` nodes (fetch
telemetry from BigQuery and evaluate it) and finally ``insight_correlation``
(cluster the sweep's failed rubrics and correlate them into the durable
insight store). This script prints the workflow events, which include that
node's per-sweep summary of new/recurring insights.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import threading
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Local-run defaults for the env vars `config.load()` requires. Set
# before importing the agent, since `config` is resolved at import time.
# `setdefault` means an exported value always wins.
# Disable `.env` loading from the agent package so the run uses only
# explicitly exported environment variables and the defaults below.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("AQA_OBSERVED_AGENT_NAME", "local-observed-agent")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "local-dev-project")

# `config.location` is the single region used for BOTH BigQuery jobs and
# the eval client. BigQuery has no `global` location, but dev
# shells routinely export `GOOGLE_CLOUD_LOCATION=global` (a valid model
# pseudo-location used by Gemini tooling), which makes BigQuery jobs fail
# with "Location global does not support this operation". For local runs,
# normalize an unset or `global` location to a real region. Export
# GOOGLE_CLOUD_LOCATION to a specific region to override this.
_DEFAULT_LOCATION = "us-central1"
_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "")
if not _location or _location.lower() == "global":
    os.environ["GOOGLE_CLOUD_LOCATION"] = _DEFAULT_LOCATION

# Keep local runs fast and cheap. Each fetched session/turn is scored by
# the judge model once per metric, so a large pull makes a local
# run take many minutes. `DATA_EVALUATION_CAP` bounds the total records
# pulled per run (budget_per_metric = cap // active_metric_count), and
# `DATA_LOOKBACK_WINDOW` bounds the telemetry window in days. Override
# either by exporting the matching env var.
os.environ.setdefault("DATA_EVALUATION_CAP", "10")
os.environ.setdefault("DATA_LOOKBACK_WINDOW", "7")

_APP_NAME = "aqa-local-run"
_USER_ID = "local-user"
_SESSION_ID = "local-session"


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for local workflow execution.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump-dir",
        help=(
            "Write the run's intermediates here: each producer's finding set"
            " verbatim, the merged findings derived from them, plus every"
            " session-review prompt and response under reviews/."
            " Diffing two runs' dumps is how a telemetry-source or prompt"
            " change gets compared."
        ),
    )
    return parser.parse_args()


def _tee_reviewer_prompts(directory: pathlib.Path) -> Callable[[], int]:
    """Record all session-review prompts and model responses under ``directory``.

    Intercepts the ``reviewer_factory`` seam, so what lands on disk is the exact
    string handed to the model. Prompts are written before the call, so a review
    that raises still leaves its prompt behind.

    Args:
        directory: Destination folder for recorded prompts and responses.

    Returns:
        Callable reporting total number of reviews captured.
    """
    from ambient_quality_agent.core.nodes import _common

    directory.mkdir(parents=True, exist_ok=True)
    original = _common.reviewer_factory
    # `review` reviews sessions concurrently, so the index needs a lock.
    lock = threading.Lock()
    captured = 0

    def create_recording_reviewer(ctx):
        call = original(ctx)

        def review(prompt: str) -> str:
            nonlocal captured
            with lock:
                index = captured
                captured += 1
            (directory / f"prompt_{index:03d}.txt").write_text(
                prompt, encoding="utf-8"
            )
            response = call(prompt)
            (directory / f"response_{index:03d}.json").write_text(
                response, encoding="utf-8"
            )
            return response

        return review

    _common.reviewer_factory = create_recording_reviewer
    return lambda: captured


def _dump_intermediates(
    directory: pathlib.Path, raw_sets: list, reviews_captured: int
) -> None:
    """Persist intermediate finding sets for offline inspection.

    Args:
        directory: Destination folder for output JSON files.
        raw_sets: List of raw finding set dictionaries.
        reviews_captured: Total count of reviewer prompts recorded.
    """
    from ambient_quality_agent.tools.insights.findings import merge_finding_sets

    directory.mkdir(parents=True, exist_ok=True)
    finding_set = merge_finding_sets(raw_sets)
    written = {
        "finding_sets.json": raw_sets,
        "findings.json": [f.model_dump() for f in finding_set.findings],
    }
    for name, payload in written.items():
        (directory / name).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            + "\n",
            encoding="utf-8",
        )
    print(
        f"\nDumped to {directory}/: {len(raw_sets)} finding set(s), "
        f"{len(finding_set.findings)} finding(s), "
        f"{reviews_captured} review prompt(s) in reviews/"
    )


async def main() -> None:
    """Seed state from `config` and run one workflow invocation."""
    args = _parse_args()
    # Imported here (not at module top) so the env defaults above are in
    # place before `ambient_quality_agent.config` is evaluated.
    from ambient_quality_agent import backends
    from ambient_quality_agent.config import config
    from ambient_quality_agent.core.investigation.job_scheduling import (
        derive_window_and_budget,
    )
    from ambient_quality_agent.core.state import WorkflowState
    from ambient_quality_agent.core.workflow import investigation_workflow
    from google.adk import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    # Storage is chosen here, as in every entry point. Without it the seams
    # stay unset and the first write raises.
    backends.configure_providers()

    # Patch the reviewer seam before the graph runs; the node resolves
    # `reviewer_factory` per call, so this reaches every review.
    reviews_captured = (
        _tee_reviewer_prompts(pathlib.Path(args.dump_dir) / "reviews")
        if args.dump_dir
        else (lambda: 0)
    )

    # Run the workflow graph directly (mirroring the deployed background
    # job in `job_execution.run_investigation_graph`), not the chat
    # orchestrator that fronts it -- so a local run exercises the pipeline
    # without needing a chat LLM backend.
    runner = Runner(
        app_name=_APP_NAME,
        node=investigation_workflow,
        session_service=InMemorySessionService(),
    )

    # The AQA workflow is state-driven: seed the typed WorkflowState from
    # the process config rather than sending a chat message. Nodes bind
    # their parameters from session state, so the state must be set on the
    # session at creation time. Derive the window and budget here (matching
    # orchestrator submission) and seed them alongside config inputs.
    window_start, window_end, budget_per_metric = derive_window_and_budget(
        config
    )
    initial_state = WorkflowState.from_config(config)
    # Local runs have no orchestrator `InvestigationRecord`, so mint a fresh run id
    # (matching the record's scheme) for run-scoped state.
    run_id = uuid.uuid4().hex[:8]
    seed_state = {
        **initial_state.model_dump(),
        "run_id": run_id,
        "window_start": window_start,
        "window_end": window_end,
        "budget_per_metric": budget_per_metric,
    }

    print(f"Creating session '{_SESSION_ID}'...")
    await runner.session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=_SESSION_ID,
        state=seed_state,
    )

    print("Running workflow...")
    print(f"  run_id              = {run_id}")
    print(f"  observed_agent_name = {initial_state.observed_agent_name}")
    print(f"  project_id          = {initial_state.project_id}")
    print(f"  location            = {initial_state.location}")
    print(f"  window              = {window_start} -> {window_end}")
    print(f"  budget_per_metric   = {budget_per_metric}")

    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=_SESSION_ID,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text="run")]
        ),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(part.text, end="")

    # Report what the workflow nodes wrote back to state.
    session = await runner.session_service.get_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=_SESSION_ID,
    )
    state = session.state if session else {}
    print("\nWorkflow results:")
    print(f"  budget_per_metric = {state.get('budget_per_metric')}")
    print(
        f"  window            = {state.get('window_start')} -> {state.get('window_end')}"
    )
    finding_sets = state.get("finding_sets") or []
    findings = sum(len(s.get("findings") or []) for s in finding_sets)
    print(
        f"  finding_sets      = {len(finding_sets)} set(s), {findings} finding(s)"
    )
    for name, value in (state.get("counters") or {}).items():
        print(f"  {name:<24} = {value}")

    if args.dump_dir:
        _dump_intermediates(
            pathlib.Path(args.dump_dir), finding_sets, reviews_captured()
        )

    # The workflow events (progress/diagnostic snippets emitted by the nodes)
    # carry the run's findings -- in particular the insight_correlation node's
    # per-sweep summary of failed rubrics grouped into new/recurring insights.
    # The insights themselves are persisted durably to the insight store.
    events = state.get("progress_log") or []
    print(f"\nWorkflow events ({len(events)}):")
    for event in events:
        print("-" * 60)
        print(str(event).strip())
    if events:
        print("-" * 60)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
