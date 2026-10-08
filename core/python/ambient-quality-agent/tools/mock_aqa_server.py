#!/usr/bin/env python3
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

"""Serve a generated AQA dataset to the dashboard, with no agent behind it.

Point the dashboard here instead of at a deployment and it cannot tell the
difference:

    python tools/mock_aqa_data.py --out scratch/mock_aqa.json
    python tools/mock_aqa_server.py --dataset scratch/mock_aqa.json &
    ADK_APP=mock_aqa tools/local_ui.sh            # http://localhost:8080

**No agent is needed, and none is wanted.** The dashboard never asks the LLM to
route a click: every action is an HTTP command route that the agent answers by
calling one tool and returning its payload. So the whole surface the UI depends
on is "route in, tool payload out" -- which is a dictionary lookup, not a model.
An ADK agent here would add a Gemini round-trip, cloud credentials and a startup
minute to a lookup table, and would make the fixture non-deterministic on top.

The chat sidebar is the one panel that *is* a model in production, and it is
mocked here anyway: its wire is deterministic and only the prose has to be
canned. Without it there is no way to iterate on the chat panel locally at all.
See "A2A" below.

What this *does* have to be faithful to is the **wire**, because that is what
the UI actually talks to. It serves the same command routes and A2A chat routes
as a locally served agent (`ambient_quality_agent.fast_api_app`), plus the ADK
dev-server's `/list-apps` probe. The dashboard's `AdkHttpClient` therefore
takes the same code path it takes against a local agent, and the UI needs no
mock-specific branch. Selecting it is one env var:
`AGENT_ADK_BASE_URL=http://localhost:8000 AQA_BACKEND=adk`.

Two behaviours are deliberately copied from the real store rather than made
convenient:

* the investigations list is capped at `LIST_LIMIT` runs, as
  `InvestigationStore.list_recent` caps it, so a chart built here against "the
  runs list" meets the same ceiling it will meet in production;
* the totals are summed over **every** run, not over that page, as
  `InvestigationStore.sum_counters` does -- which is exactly why the two can disagree,
  and why a period-scoped chart has to be built from the runs and not the
  totals.

Scheduling a run appends a `pending` record, as a real async deployment does --
nothing here can actually run an investigation. It then *settles* that record
after `SETTLE_SECONDS`, giving it the counters of the newest completed sweep.
The fiction is deliberate: the dashboard derives ▶ Run investigation from the
records and keeps it disabled while any sweep is in flight, so a mock that left
every run pending forever would demo a button that never comes back. The same
goes for the fixture's own newest sweep, which the generator leaves `running` --
see `_queue_fixture_settles`. Pass `--settle-seconds 0` to keep everything
pending and see that state on purpose. Regenerate the dataset for fresh numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import datetime as dt
import hashlib
import json
import pathlib
import random
import re
import sys
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# The route paths are a wire contract shared with the agent and the UI. Import
# them rather than restating the strings, so a path change breaks the mock at
# import instead of at the first click.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ambient_quality_shared.protocol import (
    CONFIG_ROUTE,
    GOAL_GET_ROUTE,
    GOAL_SET_ROUTE,
    GOAL_VERSIONS_ROUTE,
    HEALTH_ROUTE,
    INSIGHTS_DISMISS_ROUTE,
    INSIGHTS_GET_ROUTE,
    INSIGHTS_LIST_ROUTE,
    INSIGHTS_MERGE_ROUTE,
    INVESTIGATIONS_DAILY_ROUTE,
    INVESTIGATIONS_GET_ROUTE,
    INVESTIGATIONS_LIST_ROUTE,
    INVESTIGATIONS_SCHEDULE_ROUTE,
    INVESTIGATIONS_STATS_ROUTE,
    MEMORIES_DELETE_ROUTE,
    MEMORIES_LIST_ROUTE,
    SOURCE_ROUTE,
    TRAJECTORIES_CASE_ROUTE,
)

DATASET_VERSION = 3
"""Layout this server understands; `mock_aqa_data.py` stamps it on the file."""

LIST_LIMIT = 50
"""Runs the investigations list returns. Mirrors
`investigations.store.DEFAULT_LIST_LIMIT` -- a mock that returned more would hide
the real ceiling from whatever is being built against it."""

SETTLE_SECONDS = 40
"""How long a scheduled run stays `pending` before this server finishes it.
Longer than the dashboard's 30s poll, so the wait is watched at least once
rather than being over before the first re-read."""

STALE_AFTER_SECONDS = 30 * 60
"""When the dashboard stops waiting on a run that still calls itself in flight.
A hand-copy of `STALE_AFTER_MS` in `static/runstate.js`, used only to decide
which of the fixture's own runs this server has to finish (see
`_queue_fixture_settles`). Drift is harmless in both directions: too low and a
run finishes that was never blocking anything, too high and one is finished that
the dashboard had already written off."""

INSIGHTS_PAGE_SIZE = 30
"""Mirrors `insight_tools.INSIGHTS_PAGE_SIZE`."""

MAX_INSIGHTS_PAGE_SIZE = 100
"""Mirrors `insight_tools.MAX_INSIGHTS_PAGE_SIZE`."""

INSIGHT_ORDERS = ("recent", "impact")
"""Mirrors `reader.InsightOrder`, in the same order its error message lists."""

OCCURRENCES_PAGE_SIZE = 30
"""Mirrors `insight_tools.OCCURRENCES_PAGE_SIZE`."""

MOCK_JOBS_BUCKET = "mock-jobs"
"""Stands in for the jobs bucket in the `gs://` links the config page shows.
No such bucket exists; the documents live in the dataset."""

GOAL_OBJECT = "goal.md"
MEMORIES_PREFIX = "memories/"
"""Where the two documents live in a real deployment. Hand-copies of
`tools.documents.goal` and `tools.documents.memories`, which this server
deliberately does not import --
`tools/test_mock_aqa.py` holds the two level, so a rename cannot leave the
config page linking at a path nothing is stored under."""

MOCK_SOURCE_BUCKET = "mock-source"
"""Stands in for the source-snapshot bucket in the `gs://` link the Source card
shows. No such bucket exists; the summary rides in the dataset."""

INSIGHT_STATUSES = ("NEW", "RECURRING", "RESOLVED")
"""Lifecycle names `list_insights` accepts as a filter. A hand-copy of
`InsightStatus`, because this server deliberately imports no agent models --
`tools/test_mock_aqa.py` holds the two level so a state added to the enum
cannot leave the mock rejecting it as invalid."""

IN_FLIGHT = frozenset({"pending", "running"})
"""Run states this server counts as a sweep in progress. Hand-copied from
`RunStatus`, as `INSIGHT_STATUSES` is, and pinned by the same tests."""

WATCHING = "watching"
OVERDUE = "overdue"
NEVER = "never"
"""Verdicts `_get_ambient_health` returns. Hand-copies of the names in
`investigations.health`; `failing` is absent because nothing here decides a
sweep failed."""


def _parse_anchor(payload: dict[str, Any]) -> dt.datetime:
    """Extracts the dataset anchor timestamp with fallbacks for tests.

    Args:
        payload: Dataset dictionary containing optional `anchor` or `generated_at`.

    Returns:
        Anchor datetime in UTC.
    """
    for key in ("anchor", "generated_at"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw:
            try:
                return dt.datetime.fromisoformat(raw)
            except ValueError:
                continue
    return dt.datetime.now(tz=dt.UTC)


class Dataset:
    """The generated deployment, indexed for the reads the dashboard makes."""

    def __init__(
        self, payload: dict[str, Any], settle_seconds: int = SETTLE_SECONDS
    ) -> None:
        version = payload.get("version")
        if version != DATASET_VERSION:
            raise ValueError(
                f"dataset version {version!r} != {DATASET_VERSION}; regenerate it "
                "with tools/mock_aqa_data.py."
            )
        self.settle_seconds = settle_seconds
        # Scheduled runs still owed a finish: run_id -> (due, finished record).
        # Held here rather than on the record so nothing that goes over the wire
        # carries a field the real store has never heard of.
        self._settling: dict[str, tuple[dt.datetime, dict[str, Any]]] = {}
        self.config: dict[str, Any] = payload.get("config") or {}
        self.generated_at: str = payload.get("generated_at") or ""
        self.anchor: dt.datetime = _parse_anchor(payload)
        """When the fixture was generated, as a datetime. What the generated
        conversations hang their timestamps off, so two reads agree."""
        # Oldest first, the order `list_recent` returns and the dashboard's
        # stable tie-breaker for runs with equal timestamps.
        self.runs: list[dict[str, Any]] = sorted(
            payload.get("runs") or [], key=lambda r: r.get("created_at") or ""
        )
        self.goal: str = str(payload.get("goal") or "")
        self.goal_versions: dict[str, dict[str, str]] = {}
        """Version id -> its text and first save time, as the agent keeps them."""
        self.goal_activations: list[tuple[str, str]] = []
        """(version id, time) per save, oldest first. Ordered by position rather
        than by time, so two saves in one clock tick still list in order."""
        if self.goal:
            _record_goal_save(self, self.goal, self.anchor.isoformat())
        self.memories: list[dict[str, Any]] = list(
            payload.get("memories") or []
        )
        self.insights: list[dict[str, Any]] = list(
            payload.get("insights") or []
        )
        self.occurrences: list[dict[str, Any]] = list(
            payload.get("occurrences") or []
        )
        self.root_causes: list[dict[str, Any]] = list(
            payload.get("root_causes") or []
        )
        self.source: dict[str, Any] | None = payload.get("source") or None
        """The published snapshot's manifest summary, or None if none was."""
        self._queue_fixture_settles()

    def _queue_fixture_settles(self) -> None:
        """Finish the generator's own in-flight sweep, if it still blocks the UI.

        The fixture leaves its newest sweep `running`, and where that lands
        depends on the clock: its window closes on the cadence grid, so it is
        anywhere from seconds to hours old at serve time. Older than
        `STALE_AFTER_SECONDS` the dashboard writes it off and flags the row
        stalled, which is a state worth demoing and needs nothing from us.
        Younger, it disables ▶ Run investigation -- and nothing here would ever
        finish it, so the demo would open with a dead button for the rest of the
        half hour. Give it the same finish a scheduled run gets.
        """
        if self.settle_seconds <= 0:
            return
        cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            seconds=STALE_AFTER_SECONDS
        )
        for run in self.runs:
            if str(run.get("status") or "").lower() not in (
                "pending",
                "running",
            ):
                continue
            started = _parse_instant(
                run.get("created_at") or run.get("window_end")
            )
            if started is None or started > cutoff:
                self.finish_run_later(
                    str(run.get("run_id")),
                    _build_finished_run_fields(self.runs),
                )

    @classmethod
    def load(
        cls, path: pathlib.Path, settle_seconds: int = SETTLE_SECONDS
    ) -> Dataset:
        return cls(json.loads(path.read_text(encoding="utf-8")), settle_seconds)

    # --- investigations ---------------------------------------------------- #

    def list_recent_runs(
        self, window_start: str = "", window_end: str = ""
    ) -> list[dict[str, Any]]:
        """Lists recent investigation runs within the window without event details.

        Args:
            window_start: Optional window start boundary string.
            window_end: Optional window end boundary string.

        Returns:
            List of up to `LIST_LIMIT` run dictionaries sorted chronologically.
        """
        kept = [
            run
            for run in self.runs
            if _is_within(run.get("window_end"), window_start, window_end)
        ]
        # The cap applies *after* the period, as `list_recent`'s LIMIT does, so
        # a wide period yields its most recent page rather than a page of
        # everything filtered down to nothing.
        return [{**run, "events": []} for run in kept[-LIST_LIMIT:]]

    def sum_counters_by_day(
        self, window_start: str = "", window_end: str = ""
    ) -> list[dict[str, int | str]]:
        """Aggregates counter totals grouped by calendar day.

        Args:
            window_start: Optional window start boundary string.
            window_end: Optional window end boundary string.

        Returns:
            List of daily aggregated counter dictionaries sorted by date.
        """
        buckets: dict[str, dict[str, int | str]] = {}
        for run in self.runs:
            stamp = run.get("window_end")
            if not stamp or not _is_within(stamp, window_start, window_end):
                continue
            day = str(stamp)[:10]
            bucket = buckets.setdefault(
                day, {"day": day, "investigations": 0, "unmeasured": 0}
            )
            bucket["investigations"] = int(bucket["investigations"]) + 1
            counters = run.get("counters") or {}
            if not sum(int(v or 0) for v in counters.values()):
                bucket["unmeasured"] = int(bucket["unmeasured"]) + 1
            for name, value in counters.items():
                bucket[name] = int(bucket.get(name, 0)) + int(value or 0)
        return [buckets[day] for day in sorted(buckets)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in self.runs if r.get("run_id") == run_id), None)

    def sum_counters(
        self, window_start: str = "", window_end: str = ""
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """Sums investigation counters across matching runs and computes time coverage.

        Args:
            window_start: Optional window start boundary string.
            window_end: Optional window end boundary string.

        Returns:
            Tuple of `(totals_dict, covered_window_dict)`.
        """
        runs = [
            run
            for run in self.runs
            if _is_within(run.get("window_end"), window_start, window_end)
        ]
        totals: dict[str, int] = {"investigations": len(runs)}
        for run in runs:
            for name, value in (run.get("counters") or {}).items():
                totals[name] = totals.get(name, 0) + int(value or 0)
        windows = sorted(r["window_end"] for r in runs if r.get("window_end"))
        covered = {
            "start": windows[0] if windows else None,
            "end": windows[-1] if windows else None,
        }
        return totals, covered

    def append_run(self, run: dict[str, Any]) -> None:
        self.runs.append(run)

    def finish_run_later(self, run_id: str, finished: dict[str, Any]) -> None:
        """Queues completion fields to apply to a pending run after settle_seconds.

        Args:
            run_id: Identifier of the pending run.
            finished: Completed run fields to merge upon settlement.
        """
        if self.settle_seconds <= 0:
            return
        due = dt.datetime.now(tz=dt.UTC) + dt.timedelta(
            seconds=self.settle_seconds
        )
        self._settling[str(run_id)] = (due, finished)

    def settle_due(self, now: dt.datetime | None = None) -> int:
        """Transitions due pending runs to completed status.

        Args:
            now: Current timestamp (defaults to UTC now).

        Returns:
            Count of runs settled.
        """
        if not self._settling:
            return 0
        now = now or dt.datetime.now(tz=dt.UTC)
        settled = 0
        for run_id, (due, finished) in list(self._settling.items()):
            if due > now:
                continue
            del self._settling[run_id]
            record = self.get_run(run_id)
            if record is None:
                continue
            record.update(finished)
            record["finished_at"] = now.isoformat()
            record["updated_at"] = now.isoformat()
            record["elapsed_seconds"] = self.settle_seconds
            settled += 1
        return settled

    # --- insights ---------------------------------------------------------- #

    def get_insight(self, insight_id: str) -> dict[str, Any] | None:
        return next(
            (i for i in self.insights if i.get("insight_id") == insight_id),
            None,
        )

    def list_visible_insights(self) -> list[dict[str, Any]]:
        """Lists active insights, excluding dismissed and deduplicated entries.

        Returns:
            List of visible insight dictionaries.
        """
        return [
            i
            for i in self.insights
            if not i.get("dismissed_at") and not i.get("merged_into_insight_id")
        ]

    def resolve_owner_id(self, insight_id: str) -> str:
        """Resolves the canonical owner ID for deduplicated insight sightings.

        Args:
            insight_id: Sighting insight identifier.

        Returns:
            Canonical target insight ID if merged, otherwise the original ID.
        """
        insight = self.get_insight(insight_id)
        return str((insight or {}).get("merged_into_insight_id") or insight_id)

    def list_occurrences(self, insight_id: str) -> list[dict[str, Any]]:
        """Lists sighting occurrences belonging to an insight, ordered newest first.

        Args:
            insight_id: Canonical insight identifier.

        Returns:
            List of occurrence dictionaries sorted by created_at descending.
        """
        return sorted(
            (
                o
                for o in self.occurrences
                if self.resolve_owner_id(str(o.get("insight_id") or ""))
                == insight_id
            ),
            key=lambda o: o.get("created_at") or "",
            reverse=True,
        )

    def list_insight_ids_in_run(self, run_id: str) -> set[str]:
        """Collects canonical insight IDs observed in a specific run.

        Args:
            run_id: Investigation run identifier.

        Returns:
            Set of canonical insight ID strings.
        """
        return {
            self.resolve_owner_id(str(o.get("insight_id") or ""))
            for o in self.occurrences
            if o.get("run_id") == run_id
        }

    def list_root_causes(self, insight_id: str) -> list[dict[str, Any]]:
        """Return the newest root-cause record per sighting, newest first.

        Mirrors the reader's `QUALIFY ROW_NUMBER() OVER (PARTITION BY
        occurrence_id ORDER BY created_at DESC, root_cause_id) = 1`, so a
        fixture holding several records for one sighting yields what production
        serves rather than the full history.

        By owner, as `list_occurrences` is: a deduplication says the two insights
        are one defect, so the target answers with the duplicate's diagnoses
        beside its own and the duplicate answers with none.

        Args:
            insight_id: The ID of the insight.

        Returns:
            List of matching root-cause record dictionaries.
        """
        matching = [
            r
            for r in self.root_causes
            if self.resolve_owner_id(str(r.get("insight_id") or ""))
            == insight_id
        ]
        # Two stable passes reproduce the query's mixed-direction ordering:
        # created_at descending, root_cause_id ascending as the tie-break.
        ordered = sorted(
            matching, key=lambda r: str(r.get("root_cause_id") or "")
        )
        ordered.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        seen: set[str] = set()
        newest: list[dict[str, Any]] = []
        for record in ordered:
            occurrence_id = str(record.get("occurrence_id") or "")
            if occurrence_id in seen:
                continue
            seen.add(occurrence_id)
            newest.append(record)
        return newest

    def compute_case_index(self, case_id: str) -> int:
        """Finds the index of a conversation case within its sighting's trajectory list.

        Args:
            case_id: Evaluated conversation trajectory identifier.

        Returns:
            Integer index of the case within its sighting, or 0 if not found.
        """
        for occurrence in self.occurrences:
            ids = occurrence.get("trajectory_ids") or []
            if case_id in ids:
                return ids.index(case_id)
        return 0


# --------------------------------------------------------------------------- #
# Tool payloads                                                                #
# --------------------------------------------------------------------------- #
# One function per command route, each returning exactly what the agent-side
# tool returns.


def _list_investigations(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    # Settling is driven by reads, and this is the read the button waits on:
    # ▶ Run investigation derives its disabled state from this list and polls
    # it. Settling here rather than leaving it to whichever other route happens
    # to be polled is what keeps the button's recovery independent of which
    # cards are on screen.
    data.settle_due()
    return {
        "runs": data.list_recent_runs(
            str(args.get("window_start") or ""),
            str(args.get("window_end") or ""),
        )
    }


def _get_daily_trends(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "days": data.sum_counters_by_day(
            str(args.get("window_start") or ""),
            str(args.get("window_end") or ""),
        )
    }


def _get_investigation_stats(
    data: Dataset, args: dict[str, Any]
) -> dict[str, Any]:
    window_start = str(args.get("window_start") or "")
    window_end = str(args.get("window_end") or "")
    stats, covered = data.sum_counters(window_start, window_end)
    return {
        "stats": stats,
        # Both the period asked for and the one the counted runs cover: they
        # rarely match, and the second is what the figures are really of.
        "window": {
            "start": window_start or None,
            "end": window_end or None,
            "covered_start": covered["start"],
            "covered_end": covered["end"],
        },
    }


def _get_investigation(data: Dataset, run_id: str) -> dict[str, Any]:
    if not run_id:
        return {"error": "run_id is required"}
    run = data.get_run(run_id)
    if run is None:
        return {"error": f"No run found with id {run_id!r}."}
    return run


def _build_finished_run_fields(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Extracts completed run fields and counters from the newest completed run.

    Args:
        runs: List of existing run dictionaries.

    Returns:
        Dictionary of completed run attributes to apply to a settled run.
    """
    results: dict[str, Any] = next(
        (
            r
            for r in reversed(runs)
            if str(r.get("status") or "").lower() == "done"
        ),
        {},
    )
    return {
        "status": "done",
        "error": None,
        "counters": copy.deepcopy(results.get("counters") or {}),
        "summary": copy.deepcopy(results.get("summary")),
        "events": copy.deepcopy(results.get("events") or []),
        "metrics_passed": results.get("metrics_passed"),
        "metrics_failed": results.get("metrics_failed"),
        "metrics_errored": results.get("metrics_errored"),
    }


def _schedule_investigation(data: Dataset) -> dict[str, Any]:
    """Schedules a new pending investigation run modelled on the newest run.

    Args:
        data: Active mock dataset.

    Returns:
        Newly created pending run dictionary, or error dictionary if empty.
    """
    if not data.runs:
        return {"error": "the dataset holds no runs to model a new one on."}
    now = dt.datetime.now(tz=dt.UTC)
    template = copy.deepcopy(data.runs[-1])
    window_end = now
    window_start = now - dt.timedelta(
        seconds=int(data.config.get("ambient_cadence_seconds") or 21600)
    )
    run = {
        **template,
        "run_id": uuid.uuid4().hex[:8],
        "status": "pending",
        "trigger_type": "adhoc",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "finished_at": None,
        "elapsed_seconds": None,
        "error": None,
        "summary": None,
        "events": [],
        "counters": dict.fromkeys(template.get("counters") or {}, 0),
        "metrics_passed": None,
        "metrics_failed": None,
        "metrics_errored": None,
    }
    data.append_run(run)
    data.finish_run_later(run["run_id"], _build_finished_run_fields(data.runs))
    return run


def _get_source_snapshot(data: Dataset) -> dict[str, Any]:
    """Retrieves the source manifest summary for the observed agent.

    Args:
        data: Active mock dataset.

    Returns:
        Source snapshot dictionary with manifest URI, or unavailable status.
    """
    if data.source is None:
        return {
            "available": False,
            "reason": (
                f"No complete source snapshot has been published to "
                f"gs://{MOCK_SOURCE_BUCKET} yet. Publishing runs at deploy time, "
                "so deploy the observed agent to fill it."
            ),
        }
    revision = str(data.source.get("revision") or "")
    return {
        **data.source,
        "uri": f"gs://{MOCK_SOURCE_BUCKET}/{revision}/manifest.json",
    }


def _get_case_conversation(
    data: Dataset, args: dict[str, Any]
) -> dict[str, Any]:
    """Retrieves an archived conversation for a case with linked trajectory metadata.

    Args:
        data: Active mock dataset.
        args: Command arguments containing `trajectory_id` and optional `run_id`.

    Returns:
        Dictionary mapping "case" to the conversation structure, or error dictionary.
    """
    case_id = str(args.get("trajectory_id") or "")
    if not case_id:
        return {"error": "trajectory_id is required"}
    case = _build_conversation(case_id, data.anchor)
    run_id = str(args.get("run_id") or "")
    if run_id:
        case["trajectory"] = _build_mock_trajectory(
            run_id, case_id, data.compute_case_index(case_id)
        )
    return {"case": case}


GOAL_MAX_BYTES = 8 * 1024
"""Hand-copy of `documents.goal.GOAL_MAX_BYTES`, held level by a test."""


def _compute_goal_version(text: str) -> str:
    """Computes a 12-character SHA-256 version hash for a goal text string.

    Args:
        text: Raw goal text.

    Returns:
        12-character hex version string.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def _get_goal(data: Dataset) -> dict[str, Any]:
    return {
        "goal": data.goal or None,
        "available": True,
        "uri": f"gs://{MOCK_JOBS_BUCKET}/{GOAL_OBJECT}",
        "version": _compute_goal_version(data.goal) if data.goal else None,
    }


def _set_goal(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    """Replaces the active quality goal in the dataset.

    Every save is kept as the agent keeps it, so restoring from the Goals card
    works here too.

    Args:
        data: Active mock dataset.
        args: Command arguments containing the new `goal` string; empty
            removes the goal.

    Returns:
        Dictionary with updated goal text and version, or validation error.
    """
    goal = str(args.get("goal") or "").strip()
    if len(goal.encode("utf-8")) > GOAL_MAX_BYTES:
        return {
            "error": f"The goal is longer than {GOAL_MAX_BYTES // 1024} KiB; shorten it."
        }
    data.goal = goal
    if not goal:
        return {"goal": "", "version": None}
    _record_goal_save(data, goal, dt.datetime.now(tz=dt.UTC).isoformat())
    return {"goal": goal, "version": _compute_goal_version(goal)}


def _record_goal_save(data: Dataset, goal: str, when: str) -> None:
    """Records one save: the version if it is new, and an activation.

    Args:
        data: Active mock dataset.
        goal: Stripped goal text.
        when: ISO 8601 time of the save.
    """
    version = _compute_goal_version(goal)
    data.goal_versions.setdefault(version, {"text": goal, "created_at": when})
    data.goal_activations.append((version, when))


def _list_goal_versions(data: Dataset) -> dict[str, Any]:
    """Lists the saved goal versions, most recently active first, as the agent does.

    Args:
        data: Active mock dataset.

    Returns:
        Dictionary with the versions, each marked active or not.
    """
    last_active: dict[str, tuple[int, str]] = {
        version: (position, when)
        for position, (version, when) in enumerate(data.goal_activations)
    }
    active = _compute_goal_version(data.goal) if data.goal else None
    ordered = sorted(
        data.goal_versions,
        key=lambda v: last_active.get(v, (-1, ""))[0],
        reverse=True,
    )
    return {
        "versions": [
            {
                "version": version,
                "text": data.goal_versions[version]["text"],
                "created_at": data.goal_versions[version]["created_at"],
                "last_activated_at": last_active.get(version, (-1, ""))[1],
                "active": version == active,
            }
            for version in ordered
        ],
        "available": True,
    }


def _list_memories(data: Dataset) -> dict[str, Any]:
    """Lists the memories, newest first, as the agent does.

    Args:
        data: Active mock dataset.

    Returns:
        Dictionary with memories list, storage URI, and availability flag.
    """
    ordered = sorted(
        data.memories,
        key=lambda x: (x.get("created_at") or "", x["id"]),
        reverse=True,
    )
    return {
        "memories": [
            {
                "id": x["id"],
                "text": x["text"],
                "source": x.get("source") or "",
                "created_at": x.get("created_at") or "",
            }
            for x in ordered
        ],
        "available": True,
        "uri": f"gs://{MOCK_JOBS_BUCKET}/{MEMORIES_PREFIX}",
    }


def _delete_memory(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    """Deletes a memory from the dataset by identifier.

    Args:
        data: Active mock dataset.
        args: Command arguments containing `memory_id`.

    Returns:
        Dictionary confirming deletion, or error message.
    """
    memory_id = str(args.get("memory_id") or "")
    if not memory_id:
        return {"error": "memory_id is required"}
    remaining = [x for x in data.memories if x["id"] != memory_id]
    if len(remaining) == len(data.memories):
        return {"error": f"No memory with id {memory_id!r}."}
    data.memories = remaining
    return {"deleted": True}


def _get_ambient_health(data: Dataset) -> dict[str, Any]:
    """Calculates an ambient loop health verdict based on recent runs.

    Args:
        data: Active mock dataset.

    Returns:
        Dictionary matching the health check response schema.
    """
    data.settle_due()
    finished = [r for r in data.runs if r.get("finished_at")]
    in_flight = [r for r in data.runs if _is_running(r)]
    last_run = max(
        finished, key=lambda r: str(r.get("finished_at")), default=None
    )
    open_findings = [i for i in data.insights if i.get("status") != "RESOLVED"]
    worst = max(
        open_findings,
        key=lambda i: int(i.get("trace_count") or 0),
        default=None,
    )

    if not finished:
        verdict, reason = NEVER, "No sweep has ever finished."
    elif in_flight:
        verdict, reason = OVERDUE, f"{len(in_flight)} sweep(s) running now."
    else:
        verdict, reason = WATCHING, "No sweep is running."

    return {
        "verdict": verdict,
        "reason": reason,
        # Null for the same reason the deployment reports null: the cron lives
        # in the Cloud Scheduler job, and no server can read its own schedule.
        "schedule": None,
        "max_age_hours": None,
        "last_scheduled_finish": last_run.get("finished_at")
        if last_run
        else None,
        "last_run": _format_last_run(last_run) if last_run else None,
        "open_findings": len(open_findings),
        "worst_finding": {
            "insight_id": str(worst.get("insight_id") or ""),
            "label": str(worst.get("label") or ""),
        }
        if worst
        else None,
    }


def _is_running(run: dict[str, Any]) -> bool:
    """Checks whether a run is currently in progress and not stalled.

    Args:
        run: Run dictionary containing status and timestamps.

    Returns:
        True if the run is pending or running within STALE_AFTER_SECONDS.
    """
    if str(run.get("status") or "") not in IN_FLIGHT:
        return False
    started = _parse_instant(run.get("created_at") or run.get("window_end"))
    if started is None:
        return True
    age = (dt.datetime.now(tz=dt.UTC) - started).total_seconds()
    return age <= STALE_AFTER_SECONDS


def _format_last_run(run: dict[str, Any]) -> dict[str, Any]:
    """Formats the newest finished run into the health check summary format.

    Args:
        run: Completed run dictionary.

    Returns:
        Formatted summary dictionary with session counts and pass rate.
    """
    counters = run.get("counters") or {}
    evaluated = int(counters.get("traces_evaluated") or 0)
    passed = int(counters.get("traces_eval_passed") or 0)
    return {
        "run_id": str(run.get("run_id") or ""),
        "finished_at": run.get("finished_at"),
        "status": str(run.get("status") or ""),
        "trigger_type": str(run.get("trigger_type") or ""),
        "sessions": evaluated or None,
        "sessions_passed": passed if evaluated else None,
        "pass_rate": (passed / evaluated) if evaluated else None,
    }


def _list_insights(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    status = str(args.get("status") or "").strip()
    run_id = str(args.get("run_id") or "").strip()
    offset = _decode_page_token(str(args.get("page_token") or ""))
    order_by = str(args.get("order_by") or "").strip().lower() or "recent"
    if order_by not in INSIGHT_ORDERS:
        return {
            "error": f"invalid order_by {args.get('order_by')!r}; expected one of: "
            f"{', '.join(INSIGHT_ORDERS)}."
        }
    page_size = _resolve_page_size(args.get("page_size"))
    day = str(args.get("day") or "").strip()

    insights = data.list_visible_insights()
    wanted = "" if status.lower() in ("", "all") else status.upper()
    if wanted and wanted not in INSIGHT_STATUSES:
        return {
            "error": f"invalid status {status!r}; expected one of: "
            f"{', '.join(sorted(INSIGHT_STATUSES))}."
        }
    if day:
        # As `insight_tools._parse_day` validates and the reader filters: the
        # insights found or resolved that UTC day, the status naming which.
        if (
            not _DAY.fullmatch(day)
            or _extract_utc_day(f"{day}T00:00:00Z") != day
        ):
            return {
                "error": f"invalid day {day!r}; expected a UTC day as YYYY-MM-DD."
            }
        if wanted == "RECURRING":
            return {"error": "status RECURRING cannot be combined with a day."}
        insights = [
            i
            for i in insights
            if (
                wanted in ("", "NEW")
                and _extract_utc_day(i.get("created_at")) == day
            )
            or (
                wanted in ("", "RESOLVED")
                and _extract_utc_day(i.get("resolved_at")) == day
            )
        ]
    elif wanted:
        insights = [i for i in insights if i.get("status") == wanted]
    if run_id:
        in_run = data.list_insight_ids_in_run(run_id)
        insights = [i for i in insights if i.get("insight_id") in in_run]
    # Filter on has_root_cause matching list view indicators.
    has_root_cause = str(args.get("has_root_cause") or "")
    diagnosed = has_root_cause.strip().lower()
    if diagnosed in ("true", "false"):
        wanted_diagnosed = diagnosed == "true"
        # By owner, like the marker the filter has to agree with: an insight the
        # list shows as diagnosed is one this filter has to return.
        insights = [
            i
            for i in insights
            if bool(data.list_root_causes(str(i.get("insight_id") or "")))
            is wanted_diagnosed
        ]
    elif diagnosed:
        # Match backend validation error format for unrecognized tri-state values.
        return {
            "error": f"invalid has_root_cause {has_root_cause!r}; expected "
            "'true', 'false', or ''."
        }
    # On `updated_at` -- the last sighting -- as `_build_insight_filters` does.
    window_start = str(args.get("window_start") or "")
    window_end = str(args.get("window_end") or "")
    if window_start or window_end:
        insights = [
            i
            for i in insights
            if _is_within(i.get("updated_at"), window_start, window_end)
        ]

    # Ranked over everything that matched and only then sliced, as the ``ORDER
    # BY ... LIMIT`` does. A mock that paged an unranked list would hand the
    # dashboard a first page the deployment would never send. Impact ranks on
    # the folded `trace_count`, so the views are built before the sort.
    views = _sort_insight_views(
        [_build_insight_view(data, i) for i in insights], order_by
    )
    page = views[offset : offset + page_size]
    next_offset = offset + len(page)
    return {
        "insights": page,
        "total": len(views),
        "conversations": _count_affected_conversations(data, views),
        "next_page_token": (
            _encode_page_token(next_offset)
            if next_offset < len(views)
            else None
        ),
    }


def _count_affected_conversations(
    data: Dataset, views: list[dict[str, Any]]
) -> int:
    """Counts unique conversation trajectories across matched insight views.

    Args:
        data: Active mock dataset.
        views: List of matched insight view dictionaries.

    Returns:
        Count of distinct trajectory IDs.
    """
    seen: set[str] = set()
    for view in views:
        for occurrence in data.list_occurrences(
            str(view.get("insight_id") or "")
        ):
            seen.update(str(t) for t in occurrence.get("trajectory_ids") or [])
    return len(seen)


def _resolve_page_size(requested: Any) -> int:
    """Resolves and bounds the requested pagination page size.

    Args:
        requested: Requested page size value.

    Returns:
        Integer bounded between INSIGHTS_PAGE_SIZE and MAX_INSIGHTS_PAGE_SIZE.
    """
    try:
        size = int(requested or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return INSIGHTS_PAGE_SIZE
    return min(size, MAX_INSIGHTS_PAGE_SIZE)


def _sort_insight_views(
    views: list[dict[str, Any]], order_by: str
) -> list[dict[str, Any]]:
    """Sorts insight views by recent timestamp or impact count with ID tie-breaking.

    Args:
        views: List of insight view dictionaries.
        order_by: Sorting criterion ("recent" or "impact").

    Returns:
        Sorted list of insight view dictionaries.
    """
    if order_by == "impact":
        return sorted(
            views,
            key=lambda v: (
                -int(v.get("trace_count") or 0),
                str(v.get("insight_id") or ""),
            ),
        )
    ordered = sorted(views, key=lambda v: str(v.get("insight_id") or ""))
    ordered.sort(key=lambda v: str(v.get("updated_at") or ""), reverse=True)
    return ordered


def _build_insight_view(
    data: Dataset, insight: dict[str, Any]
) -> dict[str, Any]:
    """Builds an insight view dictionary with aggregated occurrence metrics.

    Args:
        data: Active mock dataset.
        insight: Raw stored insight record.

    Returns:
        Populated insight view dictionary.
    """
    insight_id = str(insight.get("insight_id") or "")
    owned = data.list_occurrences(insight_id)
    newest = owned[0] if owned else {}
    return {
        **insight,
        "occurrence_count": len(owned),
        "trace_count": sum(int(o.get("trace_count") or 0) for o in owned),
        "last_run_id": newest.get("run_id"),
        "last_run_at": newest.get("created_at"),
        "has_root_cause": bool(data.list_root_causes(insight_id)),
    }


def _dismiss_insight(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    """Marks an insight as dismissed in the dataset.

    Args:
        data: Active mock dataset.
        args: Command arguments containing `insight_id`.

    Returns:
        Confirmation dictionary or error message.
    """
    insight_id = str(args.get("insight_id") or "")
    if not insight_id:
        return {"error": "insight_id is required"}
    insight = data.get_insight(insight_id)
    if insight is None:
        return {"error": f"No insight found with id {insight_id!r}."}
    insight.setdefault("dismissed_at", None)
    insight["dismissed_at"] = insight["dismissed_at"] or _format_utc_now_iso()
    return {"dismissed": True}


def _merge_insight(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    """Merges an insight into a target insight, updating references in the dataset.

    Args:
        data: Active mock dataset.
        args: Command arguments with `insight_id` and `target_insight_id`.

    Returns:
        Confirmation dictionary or error message.
    """
    insight_id = str(args.get("insight_id") or "")
    target_id = str(args.get("target_insight_id") or "")
    if not insight_id:
        return {"error": "insight_id is required"}
    if not target_id:
        return {"error": "target_insight_id is required"}
    if insight_id == target_id:
        return {"error": "an insight cannot be a duplicate of itself"}
    target = data.get_insight(target_id)
    refused = (
        data.get_insight(insight_id) is None
        or target is None
        or bool(target.get("merged_into_insight_id"))
    )
    if refused:
        return {
            "error": (
                f"Could not merge {insight_id!r} into {target_id!r}: "
                "one of them does not exist, or the target is itself a duplicate."
            )
        }
    now = _format_utc_now_iso()
    # The source and everything already pointing at it, which is the one
    # statement the deployment sends rather than a read followed by a write.
    for insight in data.insights:
        if insight.get("insight_id") == insight_id or (
            insight.get("merged_into_insight_id") == insight_id
        ):
            insight["merged_into_insight_id"] = target_id
            insight["merged_at"] = now
    return {"merged": True}


def _get_insight(data: Dataset, args: dict[str, Any]) -> dict[str, Any]:
    insight_id = str(args.get("insight_id") or "")
    if not insight_id:
        return {"error": "insight_id is required"}
    insight = data.get_insight(insight_id)
    if insight is None:
        return {"error": f"No insight found with id {insight_id!r}."}

    occurrences = data.list_occurrences(insight_id)
    if run_id := str(args.get("run_id") or ""):
        occurrences = [o for o in occurrences if o.get("run_id") == run_id]
    offset = _decode_page_token(str(args.get("page_token") or ""))
    page = occurrences[offset : offset + OCCURRENCES_PAGE_SIZE]
    next_offset = offset + len(page)

    include_traces = bool(args.get("include_traces", True))
    resolve = args.get("include_trajectories")
    include_trajectories = include_traces if resolve is None else bool(resolve)
    include_edits = bool(args.get("include_edits", True))
    root_causes = data.list_root_causes(insight_id)
    return {
        # Not filtered the way the list is: a bookmarked link to a dismissed or
        # deduplicated insight still resolves, as `get_insight` does.
        "insight": _build_insight_view(data, insight),
        "occurrences": [
            _format_occurrence(
                o,
                include_traces,
                include_trajectories,
                root_causes,
                include_edits,
            )
            for o in page
        ],
        # Return all insight root causes so diagnoses survive occurrence ID regeneration on retry.
        "root_causes": [
            _format_root_cause(r, include_edits) for r in root_causes
        ],
        "next_page_token": (
            _encode_page_token(next_offset)
            if next_offset < len(occurrences)
            else None
        ),
    }


MOCK_PROJECT = "mock-aqa-project"
"""Project the generated console links point at, matching the rest of the mock."""

_TRACE_CONSOLE_BASE = "https://console.cloud.google.com/traces/list"


def _format_occurrence(
    occurrence: dict[str, Any],
    include_traces: bool,
    include_trajectories: bool,
    root_causes: Iterable[dict[str, Any]] = (),
    include_edits: bool = True,
) -> dict[str, Any]:
    """Format an occurrence dictionary for API responses.

    Strips heavy rubric traces, resolves trajectory conversations, and attaches
    sighting-specific root-cause records.

    Args:
        occurrence: Raw occurrence record.
        include_traces: Whether to include rubric traces.
        include_trajectories: Whether to resolve trajectory data.
        root_causes: Root-cause records to filter and attach.
        include_edits: Whether to keep code diffs in attached records.

    Returns:
        Formatted occurrence view dictionary.
    """
    view = copy.deepcopy(occurrence)
    if not include_traces:
        for rubric in view.get("rubrics") or []:
            rubric["trace"] = []
    if include_trajectories:
        view["trajectories"] = [
            _build_mock_trajectory(
                occurrence.get("run_id") or "", case_id, index
            )
            for index, case_id in enumerate(
                occurrence.get("trajectory_ids") or []
            )
        ]
    view["root_causes"] = [
        _format_root_cause(record, include_edits)
        for record in root_causes
        if record.get("occurrence_id") == occurrence.get("occurrence_id")
    ]
    return view


def _format_root_cause(
    record: dict[str, Any], include_edits: bool
) -> dict[str, Any]:
    """Format a root-cause record for API responses.

    Args:
        record: Raw root-cause record.
        include_edits: Whether to retain 'before' and 'after' code snippets in edits.

    Returns:
        Formatted root-cause dictionary with 'edit_count' and optionally stripped diffs.
    """
    view = copy.deepcopy(record)
    edits = view.get("edits") or []
    view["edit_count"] = len(edits)
    if not include_edits:
        view["edits"] = [
            {
                key: value
                for key, value in edit.items()
                if key not in ("before", "after")
            }
            for edit in edits
        ]
    return view


def _build_mock_trajectory(
    run_id: str, case_id: str, index: int
) -> dict[str, Any]:
    """Builds a mock trajectory record with console links.

    Args:
        run_id: Investigation run identifier.
        case_id: Conversation trajectory identifier.
        index: Index of the case within the occurrence.

    Returns:
        Dictionary matching the linked trajectory schema.
    """
    linkable = index % 3 != 2
    trace_ids = [f"{abs(hash((case_id, index))):032x}"[:32]] if linkable else []
    return {
        "run_id": run_id,
        "trajectory_id": case_id,
        "source": "cloud_logging" if linkable else "big_query",
        "source_trace_ids": trace_ids,
        "ingest_status": "ingested",
        "console_url": (
            f"{_TRACE_CONSOLE_BASE}?project={MOCK_PROJECT}&tid={trace_ids[0]}"
            if trace_ids
            else ""
        ),
    }


PAYLOAD_STATUSES = ("ingested", "partial", "truncated")
"""What a stored copy of a conversation is. A hand-copy of `PayloadStatus`, as
the other vocabularies in this file are, pinned by `tools/test_mock_aqa.py`."""

NOT_ARCHIVED = "not_archived"
"""And what an id with no stored copy is. Outside the enum above, deliberately:
`payloads.NOT_ARCHIVED` is the agent's spelling and the same test holds them
level."""

_TOOLS = (
    ("lookup_travel_policy", {"topic": "per-diem", "region": "EMEA"}),
    ("get_employee", {"employee_id": "E-4471"}),
    ("file_expense", {"category": "Airfare", "amount_eur": 412.9}),
)


def _build_conversation(case_id: str, anchor: dt.datetime) -> dict[str, Any]:
    """Builds a mock archived conversation trace for a specific case ID.

    Args:
        case_id: Unique conversation identifier.
        anchor: Reference timestamp anchoring event times.

    Returns:
        Dictionary matching the archived conversation schema.
    """
    rng = random.Random(f"conversation:{case_id}")  # noqa: S311 - seeded for deterministic mock data
    ordinal = int(
        hashlib.sha1(case_id.encode(), usedforsecurity=False).hexdigest()[:8],
        16,
    )
    started = anchor - dt.timedelta(hours=rng.uniform(1, 400))

    if ordinal % 7 == 0:
        return {
            "trajectory_id": case_id,
            "status": NOT_ARCHIVED,
            "turn_count": 0,
            "turns_returned": 0,
            "turns": [],
            "agents": None,
            "recorded_at": None,
        }

    turns = _build_conversation_turns(rng, started)
    expired = ordinal % 11 == 0
    return {
        "trajectory_id": case_id,
        # Mostly whole; every seventeenth copy came from lossy telemetry.
        "status": "partial" if ordinal % 17 == 0 else "ingested",
        "turn_count": len(turns),
        "turns_returned": 0 if expired else len(turns),
        "turns": [] if expired else turns,
        "agents": {
            "travel_desk_agent": {
                "instruction": (
                    "Book travel and file expenses. Use your tools for anything "
                    "factual; never answer policy or prices from memory."
                ),
                "tools": [name for name, _ in _TOOLS],
            }
        },
        "recorded_at": started.isoformat(),
    }


def _build_conversation_turns(
    rng: random.Random, started: dt.datetime
) -> list[dict[str, Any]]:
    """Generates dialogue turns simulating user interactions and tool execution.

    Args:
        rng: Seeded random number generator.
        started: Conversation start timestamp.

    Returns:
        List of turn dictionaries.
    """
    tool_name, args = rng.choice(_TOOLS)
    failed = rng.random() < 0.25
    clock = started

    def build_event(author: str, part: dict[str, Any]) -> dict[str, Any]:
        """Creates a single dialogue event, advancing the simulated clock.

        Args:
            author: Event author ("user" or agent name).
            part: Content part dictionary (text, function_call, etc.).

        Returns:
            Formatted event dictionary.
        """
        nonlocal clock
        clock += dt.timedelta(seconds=rng.uniform(0.4, 3.0))
        return {
            "author": author,
            "content": {
                "role": "user" if author == "user" else "model",
                "parts": [part],
            },
            "event_time": clock.isoformat(),
        }

    call_id = f"call-{rng.randrange(16**8):08x}"
    response = (
        {"error": "PERMISSION_DENIED: the policy table is not readable"}
        if failed
        else {"result": "EUR 62/day for EMEA, effective 2026-07-01"}
    )
    turns: list[dict[str, Any]] = [
        {
            "turn_index": 0,
            "turn_id": f"t-{rng.randrange(16**6):06x}",
            "events": [
                build_event(
                    "user",
                    {"text": "What's the per-diem for Dublin next week?"},
                ),
                build_event(
                    "travel_desk_agent", {"text": "Let me check the policy."}
                ),
                build_event(
                    "travel_desk_agent",
                    {
                        "function_call": {
                            "id": call_id,
                            "name": tool_name,
                            "args": args,
                        }
                    },
                ),
                build_event(
                    "travel_desk_agent",
                    {
                        "function_response": {
                            "id": call_id,
                            "name": tool_name,
                            "response": response,
                        }
                    },
                ),
                build_event(
                    "travel_desk_agent",
                    {
                        "text": (
                            "I couldn't reach the policy table, but it is usually "
                            "about EUR 60."
                            if failed
                            else "The per-diem for Dublin is EUR 62 a day."
                        )
                    },
                ),
            ],
        },
        {
            "turn_index": 1,
            "turn_id": f"t-{rng.randrange(16**6):06x}",
            "events": [
                build_event(
                    "user", {"text": "And can you file yesterday's flight?"}
                ),
                # An agent turn with no text and no tool call is the
                # `empty_final_response` signal, which the timeline draws as its
                # own item rather than as silence.
                build_event("travel_desk_agent", {"text": ""})
                if rng.random() < 0.2
                else build_event(
                    "travel_desk_agent", {"text": "Filed under Airfare."}
                ),
            ],
        },
    ]
    return turns


def _encode_page_token(offset: int) -> str:
    """Encodes an integer pagination offset into a URL-safe base64 token.

    Args:
        offset: Integer record offset.

    Returns:
        URL-safe base64 encoded token string.
    """
    return base64.urlsafe_b64encode(
        json.dumps({"offset": offset}).encode()
    ).decode()


def _decode_page_token(token: str) -> int:
    if not token:
        return 0
    try:
        return max(
            0,
            int(json.loads(base64.urlsafe_b64decode(token.encode()))["offset"]),
        )
    except Exception:
        return 0


async def _read_route_args(request: Request) -> dict[str, Any]:
    """Parses JSON arguments from an incoming HTTP request body.

    Args:
        request: Incoming HTTP request.

    Returns:
        Parsed JSON dictionary, or empty dictionary if body is empty or invalid.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    args = json.loads(raw)
    return args if isinstance(args, dict) else {}


def _is_within(stamp: str | None, start: str, end: str) -> bool:
    """Checks whether an ISO timestamp falls within optional start and end bounds.

    Args:
        stamp: ISO timestamp string to evaluate, or None.
        start: Optional start boundary string.
        end: Optional end boundary string.

    Returns:
        True if the timestamp falls within [start, end].
    """
    if not start and not end:
        return True
    when = _parse_instant(stamp)
    if when is None:
        return False
    lower, upper = _parse_instant(start), _parse_instant(end)
    if lower is not None and when < lower:
        return False
    return not (upper is not None and when > upper)


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _extract_utc_day(value: str | None) -> str | None:
    """Extracts the UTC date string (YYYY-MM-DD) from an ISO timestamp.

    Args:
        value: ISO timestamp string, or None.

    Returns:
        UTC date string, or None if input is invalid or absent.
    """
    when = _parse_instant(value)
    return when.astimezone(dt.UTC).date().isoformat() if when else None


def _parse_instant(value: str | None) -> dt.datetime | None:
    """Parses an ISO timestamp string into a timezone-aware UTC datetime.

    Args:
        value: ISO timestamp string, or None.

    Returns:
        UTC datetime object, or None if parsing fails or input is empty.
    """
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _format_sse_frame(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


# --------------------------------------------------------------------------- #
# A2A: the chat sidebar                                                        #
# --------------------------------------------------------------------------- #
# The dashboard's chat is not a command route -- it is an A2A client
# (`ui/web/lib/a2a-client.ts`, on `@a2a-js/sdk`) talking JSON-RPC over
# same-origin `/a2a`, which `ambient_quality_ui/app.py` forwards here as
# `/a2a/<chat app>`. Under `mock_ui.sh` the UI resolves an `AdkHttpClient`
# backend, so the proxy attaches no token and this server is the upstream.
#
# The client omits `legacyCompat`, which selects the **native 1.0** transport:
# the methods are `SendStreamingMessage` / `GetTask` / `CancelTask`, not the
# v0.3 `message/stream` spelling. Both are accepted below, because the UI
# proxy's own `_STREAMING_METHODS` carries both dialects and its artifact
# repair issues a `GetTask` of its own.
#
# Three shapes have to be right or the panel fails in ways that read as
# something else:
#
# * the card's `supportedInterfaces` must be **non-empty** -- the client throws
#   a worded "backend predates A2A 1.0" before it builds a transport. Its `url`
#   is overwritten with `/a2a` client-side, so the value only has to survive
#   the proxy's `_rewrite_card_to_same_origin` rewrite;
# * the prose rides on **artifactUpdate** frames, as ADK's own converter emits
#   it. A turn that carries none makes the proxy fetch the finished task and
#   synthesize them (`_fetch_task_artifact_frames`), so text streamed as status
#   messages alone would arrive twice;
# * a frame carrying a terminal state ends the turn. `TASK_STATE_INPUT_REQUIRED`
#   counts as terminal for the client -- the turn is over until the user
#   answers -- which is what makes the confirmation card reachable.

A2A_PROTOCOL_VERSION = "1.0"
"""Protocol version the card advertises. `AGENT_CARD_TIMEOUT_MS` aside, this is
the one field the client's transport picker matches on."""

A2A_PROTOCOL_BINDING = "JSONRPC"
"""`pickMatchingInterface` compares this case-insensitively against the
transport's own name; anything else leaves the client with no transport."""

CONFIRMATION_TOOL = "adk_request_confirmation"
"""ADK's framework-synthesized human-in-the-loop tool -- a hand-copy of its
`REQUEST_CONFIRMATION_FUNCTION_CALL_NAME`, which `horizon-events.ts` copies
too. It is the only name the dashboard renders as an approve/decline card
rather than as a generic tool row."""

CONFIRM_KEYWORD = "sweep"
"""What a user says to get the confirmation card. Outside a deployment there is
no other way to see it, so the mock puts it behind a word rather than behind a
tool the agent would have to be able to run."""

REMEMBER_PREFIX = "remember:"
"""A message starting with this stores the rest as a memory, as the agent's
`remember` does when the developer asks it to remember something."""

MAX_MEMORY_CHARS = 500
"""`documents.memories.MAX_MEMORY_CHARS`, held level by `tools/test_mock_aqa.py`."""

GOAL_PREFIX = "goal:"
"""A message starting with this asks the mock to save the rest as the goal, so
the goal approval card can be seen without a model to decide to call
`set_goal`."""

GOAL_REMOVALS = ("remove goal", "remove the goal")
"""Messages that ask the mock to remove the goal, through the same card."""

CHUNK_WORDS = 5
"""Words per artifact frame. A reply has to arrive in several frames or the
typing indicator and the mid-turn cancel are never exercised."""


def _extract_a2a_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    parts = message.get("parts")
    return (
        [p for p in parts if isinstance(p, dict)]
        if isinstance(parts, list)
        else []
    )


def _extract_a2a_text(message: dict[str, Any]) -> str:
    """Extracts plain text from text parts of an A2A message dictionary.

    Args:
        message: A2A message dictionary.

    Returns:
        Concatenated text string with whitespace normalized.
    """
    return " ".join(
        str(p["text"])
        for p in _extract_a2a_parts(message)
        if isinstance(p.get("text"), str)
    ).strip()


def _extract_confirmation_answer(
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Extracts human-in-the-loop confirmation response data from an A2A message.

    Args:
        message: A2A message dictionary.

    Returns:
        Function response dictionary if confirmation tool was answered, else None.
    """
    for part in _extract_a2a_parts(message):
        if (part.get("metadata") or {}).get("adk_type") != "function_response":
            continue
        data = part.get("data")
        if isinstance(data, dict) and data.get("name") == CONFIRMATION_TOOL:
            return data
    return None


def _parse_goal_request(text: str) -> str | None:
    """The goal a chat message asks to save, empty to remove it, or None.

    Args:
        text: The user's message.

    Returns:
        The goal after `GOAL_PREFIX`, "" for one of `GOAL_REMOVALS`, or None
        when the message asks neither.
    """
    if text.lower().startswith(GOAL_PREFIX):
        return text[len(GOAL_PREFIX) :].strip()
    if text.strip().lower().rstrip(".") in GOAL_REMOVALS:
        return ""
    return None


def _split_into_chunks(prose: str) -> list[str]:
    """Splits prose into word chunks preserving whitespace for SSE frame streaming.

    Args:
        prose: Text string to split.

    Returns:
        List of text chunks.
    """
    words = prose.split(" ")
    return [
        " ".join(words[i : i + CHUNK_WORDS])
        + ("" if i + CHUNK_WORDS >= len(words) else " ")
        for i in range(0, len(words), CHUNK_WORDS)
    ] or [prose]


def _format_utc_now_iso() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat()


class Chat:
    """The canned chat agent: canned prose, real wire, real task records.

    Tasks are kept in memory so `GetTask` can replay a turn and a confirmation
    can find the task it answers. A restart losing them is intended -- the
    fiction here is a conversation, not a store.
    """

    def __init__(self, data: Dataset) -> None:
        self.data = data
        self.tasks: dict[str, dict[str, Any]] = {}
        self.pending_goals: dict[str, str] = {}
        """The goal each unanswered goal card would save, by its call id."""

    # --- the turn ---------------------------------------------------------- #

    def run_turn(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Processes a user message turn and generates sequential SSE frame results.

        Args:
            message: Incoming A2A user message dictionary.

        Returns:
            List of JSON-RPC frame result dictionaries.
        """
        context_id = str(message.get("contextId") or uuid.uuid4().hex[:16])
        answered = _extract_confirmation_answer(message)
        # Every message opens a task of its own, the answer to a confirmation
        # included: `sendConfirmation` sends `taskId: ""`, so it is not asking
        # to continue the task that asked. Continuing it anyway makes the
        # answer's reply vanish -- the history-versus-live merge drops an
        # optimistic bubble as soon as a canonical copy of its task lands, and
        # the asking task already had one. The card still resolves on a reload:
        # the answer rides in this task's history as the `function_response`
        # `resolveHitlSegments` matches to it by call id.
        task = self._create_task(context_id)
        task["history"].append(copy.deepcopy(message))
        task_id = task["id"]

        prose, confirmation = self._resolve_answer(
            _extract_a2a_text(message), answered, context_id=context_id
        )
        frames: list[dict[str, Any]] = [
            {"task": {k: task[k] for k in ("id", "contextId", "status")}},
            {
                "statusUpdate": self._build_status_update(
                    task_id, context_id, "TASK_STATE_WORKING"
                )
            },
        ]

        artifact = {
            "artifactId": f"a-{uuid.uuid4().hex[:8]}",
            "name": "response",
            "parts": [{"text": prose}],
        }
        chunks = _split_into_chunks(prose)
        for index, chunk in enumerate(chunks):
            frames.append(
                {
                    "artifactUpdate": {
                        "taskId": task_id,
                        "contextId": context_id,
                        "artifact": {**artifact, "parts": [{"text": chunk}]},
                        "append": index > 0,
                        "lastChunk": index == len(chunks) - 1,
                    }
                }
            )
        task.setdefault("artifacts", []).append(artifact)

        if confirmation is None:
            task["status"] = self._build_status_update(
                task_id, context_id, "TASK_STATE_COMPLETED"
            )["status"]
            frames.append(
                {
                    "statusUpdate": self._build_status_update(
                        task_id, context_id, "TASK_STATE_COMPLETED"
                    )
                }
            )
            return frames

        # The card rides on the status message rather than on an artifact:
        # `task-to-segments` only looks for it there, and only while the task
        # is INPUT_REQUIRED, so a post-turn history refetch re-renders it.
        pending = self._build_status_update(
            task_id,
            context_id,
            "TASK_STATE_INPUT_REQUIRED",
            message=self._build_agent_message(
                task_id, context_id, [confirmation]
            ),
        )
        task["status"] = pending["status"]
        frames.append({"statusUpdate": pending})
        return frames

    # --- the canned answers ------------------------------------------------ #

    def _resolve_answer(
        self,
        text: str,
        answered: dict[str, Any] | None,
        *,
        context_id: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        """Resolves canned prose and optional confirmation requests for a message.

        Args:
            text: Normalized user message text.
            answered: Optional confirmation answer payload.
            context_id: The chat the message belongs to, which records any
                memory it asks for.

        Returns:
            Tuple of `(response_prose, optional_confirmation_dict)`.
        """
        if answered is not None:
            return self._apply_confirmation_answer(answered), None
        if text.lower().startswith(REMEMBER_PREFIX):
            return self._remember(
                text[len(REMEMBER_PREFIX) :], context_id
            ), None
        goal = _parse_goal_request(text)
        if goal is not None:
            return (
                "Here is what I would save; nothing changes until you approve it."
                if goal
                else "Removing the goal needs your approval first.",
                self._build_goal_confirmation_request(goal),
            )
        asked = text.lower()
        if CONFIRM_KEYWORD in asked:
            return (
                "An ad-hoc sweep costs a full evaluation pass over the window, "
                "so it needs your say-so first.",
                self._build_confirmation_request(),
            )
        if "insight" in asked or "issue" in asked or "problem" in asked:
            return self._render_top_insights(), None
        if "run" in asked or "investigation" in asked or "sweep" in asked:
            return self._render_newest_run(), None
        return self._render_orientation(), None

    def _remember(self, text: str, source: str) -> str:
        """Stores a memory, as the agent's `remember` does.

        Args:
            text: The memory's text.
            source: The chat recording it.

        Returns:
            The reply prose.
        """
        cleaned = text.strip()
        if not cleaned or len(cleaned) > MAX_MEMORY_CHARS:
            return (
                f"A memory needs text, at most {MAX_MEMORY_CHARS} characters."
            )
        memory_id = hashlib.sha256(cleaned.encode()).hexdigest()[:12]
        existing = next(
            (x for x in self.data.memories if x["id"] == memory_id), None
        )
        if existing is not None:
            return "That memory is already stored."
        self.data.memories.append(
            {
                "id": memory_id,
                "text": cleaned,
                "source": source,
                "created_at": _format_utc_now_iso(),
            }
        )
        return (
            "Remembered. You can delete it on the Memory card of the "
            "Configuration page."
        )

    def _render_top_insights(self) -> str:
        ranked = sorted(
            self.data.insights,
            key=lambda i: int(i.get("trace_count") or 0),
            reverse=True,
        )[:3]
        if not ranked:
            return "No insights in this dataset -- it was generated with nothing failing."
        lines = "\n".join(
            f"{n}. **{i.get('label')}** -- {i.get('trace_count')} traces over "
            f"{i.get('occurrence_count')} sightings, {i.get('status')}."
            for n, i in enumerate(ranked, start=1)
        )
        return f"The {len(ranked)} insights with the most traces behind them:\n\n{lines}"

    def _render_newest_run(self) -> str:
        run = self.data.runs[-1] if self.data.runs else None
        if run is None:
            return "This dataset holds no sweeps."
        counters = run.get("counters") or {}
        return (
            f"The newest sweep is `{run.get('run_id')}` ({run.get('status')}), "
            f"triggered {run.get('trigger_type')} and covering up to "
            f"{run.get('window_end')}. It scanned "
            f"{counters.get('traces_scanned', 0)} traces, evaluated "
            f"{counters.get('traces_evaluated', 0)} of them, and "
            f"{counters.get('traces_eval_failed', 0)} failed a metric."
        )

    def _render_orientation(self) -> str:
        return (
            "I am the mock chat: canned prose over the real A2A wire, with no "
            f"model behind me. This deployment has {len(self.data.runs)} sweeps and "
            f"{len(self.data.insights)} insights. Ask about **insights** or the "
            f"latest **run** for figures off the same dataset the panels read, "
            f"say **{CONFIRM_KEYWORD}** to see the approval card, start a "
            f"message with **{GOAL_PREFIX}** to save a goal, or with "
            f"**{REMEMBER_PREFIX}** to store a memory."
        )

    def _build_confirmation_request(self) -> dict[str, Any]:
        """Builds an approve/decline tool confirmation request data part.

        Returns:
            A2A data part dictionary representing the confirmation request.
        """
        return {
            "data": {
                "id": f"call-{uuid.uuid4().hex[:8]}",
                "name": CONFIRMATION_TOOL,
                "args": {
                    "originalFunctionCall": {
                        "name": "schedule_investigation",
                        "args": {"trigger_type": "adhoc"},
                    },
                    "toolConfirmation": {
                        "hint": "Start an ad-hoc sweep over the current window?",
                        "payload": None,
                    },
                },
            },
            "metadata": {"adk_type": "function_call"},
        }

    def _build_goal_confirmation_request(self, goal: str) -> dict[str, Any]:
        """Builds the approval card the agent's `set_goal` asks for.

        Args:
            goal: The goal to save; empty to remove it.

        Returns:
            A2A data part dictionary representing the confirmation request.
        """
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        self.pending_goals[call_id] = goal
        return {
            "data": {
                "id": call_id,
                "name": CONFIRMATION_TOOL,
                "args": {
                    "originalFunctionCall": {
                        "name": "set_goal",
                        "args": {"goal": goal},
                    },
                    "toolConfirmation": {
                        "hint": "Save this as the developer goal?"
                        if goal
                        else "Remove the developer goal?",
                        "payload": None,
                    },
                },
            },
            "metadata": {"adk_type": "function_call"},
        }

    def _apply_confirmation_answer(self, answered: dict[str, Any]) -> str:
        response = answered.get("response")
        confirmed = (
            bool(response.get("confirmed"))
            if isinstance(response, dict)
            else False
        )
        call_id = str(answered.get("id") or "")
        if call_id in self.pending_goals:
            return self._apply_goal_answer(
                self.pending_goals.pop(call_id), confirmed
            )
        if not confirmed:
            return "Left it alone, then. Nothing was scheduled."
        run = _schedule_investigation(self.data)
        # A refusal is `{"error": ...}` and nothing else; a run record carries
        # an `error` key of its own, set to None, so its presence proves nothing.
        if "run_id" not in run:
            return f"Could not schedule it: {run.get('error')}"
        return (
            f"Scheduled. Sweep `{run['run_id']}` is pending; the investigations "
            "list will show it finish."
        )

    def _apply_goal_answer(self, goal: str, confirmed: bool) -> str:
        if not confirmed:
            return "Left the goal as it was."
        result = _set_goal(self.data, {"goal": goal})
        if "error" in result:
            return f"Could not save it: {result['error']}"
        if not goal:
            return (
                "Removed the goal. It stays in Previous versions on the "
                "Configuration page."
            )
        return f"Saved. The goal is now version `{result['version']}`."

    # --- task records ------------------------------------------------------ #

    def _create_task(self, context_id: str) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:16]
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {
                "state": "TASK_STATE_SUBMITTED",
                "timestamp": _format_utc_now_iso(),
            },
            "artifacts": [],
            "history": [],
        }
        self.tasks[task_id] = task
        return task

    def _build_status_update(
        self,
        task_id: str,
        context_id: str,
        state: str,
        message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status: dict[str, Any] = {
            "state": state,
            "timestamp": _format_utc_now_iso(),
        }
        if message is not None:
            status["message"] = message
        return {"taskId": task_id, "contextId": context_id, "status": status}

    def _build_agent_message(
        self, task_id: str, context_id: str, parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "messageId": uuid.uuid4().hex[:16],
            "contextId": context_id,
            "taskId": task_id,
            "role": "ROLE_AGENT",
            "parts": parts,
        }

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task["status"] = {
            "state": "TASK_STATE_CANCELED",
            "timestamp": _format_utc_now_iso(),
        }
        return task


def _build_agent_card(base_url: str, a2a_app: str) -> dict[str, Any]:
    """Constructs the A2A agent card advertising supported interfaces.

    Args:
        base_url: Base URL where the mock server is hosted.
        a2a_app: Registered A2A application name.

    Returns:
        Agent card dictionary adhering to the A2A 1.0 specification.
    """
    return {
        "name": a2a_app,
        "description": (
            "The AQA mock chat. Canned prose over the real A2A wire, answering "
            "from the generated dataset."
        ),
        "version": "0.0.0-mock",
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "supportedInterfaces": [
            {
                "url": f"{base_url}/a2a/{a2a_app}",
                "protocolBinding": A2A_PROTOCOL_BINDING,
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }
        ],
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


# The v1.0 method names the client sends, and the v0.3 spelling the UI proxy
# still recognises, mapped onto what this server does about each.
A2A_STREAMING_METHODS = frozenset({"SendStreamingMessage", "message/stream"})
A2A_GET_TASK_METHODS = frozenset({"GetTask", "tasks/get"})
A2A_CANCEL_TASK_METHODS = frozenset({"CancelTask", "tasks/cancel"})


def _build_rpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _build_rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message},
    }


def build_app(data: Dataset, *, app_name: str, latency_ms: int) -> FastAPI:
    """Constructs and configures the mock FastAPI application.

    Args:
        data: Indexed mock dataset instance.
        app_name: Application name to advertise on ADK discovery routes.
        latency_ms: Delay in milliseconds between streamed A2A chat frames.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(title="AQA mock agent server")
    chat = Chat(data)

    @app.get("/list-apps")
    async def list_apps() -> list[str]:
        # `tools/local_ui.sh` probes this both to detect a running server and to
        # check the app name it was told to use.
        return [app_name]

    # The dashboard drives its panels over the command routes, so the mock
    # serves the same paths the container does. Each one reuses the reader the
    # readers the container's own routes call, so both answer the same way.
    @app.post(f"/{INVESTIGATIONS_LIST_ROUTE}")
    async def list_investigations(request: Request) -> dict[str, Any]:
        return _list_investigations(data, await _read_route_args(request))

    @app.post(f"/{INVESTIGATIONS_STATS_ROUTE}")
    async def get_investigation_stats(request: Request) -> dict[str, Any]:
        return _get_investigation_stats(data, await _read_route_args(request))

    @app.post(f"/{INVESTIGATIONS_DAILY_ROUTE}")
    async def get_daily_trends(request: Request) -> dict[str, Any]:
        return _get_daily_trends(data, await _read_route_args(request))

    @app.post(f"/{INVESTIGATIONS_SCHEDULE_ROUTE}")
    async def schedule_investigation() -> dict[str, Any]:
        return _schedule_investigation(data)

    @app.post(f"/{INVESTIGATIONS_GET_ROUTE}")
    async def get_investigation(request: Request) -> dict[str, Any]:
        args = await _read_route_args(request)
        return _get_investigation(data, str(args.get("run_id", "")))

    @app.post(f"/{INSIGHTS_LIST_ROUTE}")
    async def list_insights(request: Request) -> dict[str, Any]:
        return _list_insights(data, await _read_route_args(request))

    @app.post(f"/{INSIGHTS_GET_ROUTE}")
    async def get_insight(request: Request) -> dict[str, Any]:
        return _get_insight(data, await _read_route_args(request))

    @app.post(f"/{INSIGHTS_DISMISS_ROUTE}")
    async def dismiss_insight(request: Request) -> dict[str, Any]:
        return _dismiss_insight(data, await _read_route_args(request))

    @app.post(f"/{INSIGHTS_MERGE_ROUTE}")
    async def merge_insight(request: Request) -> dict[str, Any]:
        return _merge_insight(data, await _read_route_args(request))

    @app.post(f"/{GOAL_GET_ROUTE}")
    async def get_goal() -> dict[str, Any]:
        return _get_goal(data)

    @app.post(f"/{GOAL_SET_ROUTE}")
    async def set_goal(request: Request) -> dict[str, Any]:
        return _set_goal(data, await _read_route_args(request))

    @app.post(f"/{GOAL_VERSIONS_ROUTE}")
    async def route_list_goal_versions() -> dict[str, Any]:
        return _list_goal_versions(data)

    @app.post(f"/{MEMORIES_LIST_ROUTE}")
    async def list_memories() -> dict[str, Any]:
        return _list_memories(data)

    @app.post(f"/{MEMORIES_DELETE_ROUTE}")
    async def delete_memory(request: Request) -> dict[str, Any]:
        return _delete_memory(data, await _read_route_args(request))

    @app.post(f"/{TRAJECTORIES_CASE_ROUTE}")
    async def get_case_conversation(request: Request) -> dict[str, Any]:
        return _get_case_conversation(data, await _read_route_args(request))

    @app.post(f"/{HEALTH_ROUTE}")
    async def get_ambient_health() -> dict[str, Any]:
        return _get_ambient_health(data)

    @app.post(f"/{SOURCE_ROUTE}")
    async def get_source_snapshot() -> dict[str, Any]:
        return _get_source_snapshot(data)

    @app.post(f"/{CONFIG_ROUTE}")
    async def show_config() -> dict[str, Any]:
        return {"config": data.config}

    # The chat agent's name is its callers' business, not this server's, so it
    # is a path parameter rather than a hand-copy of `CHAT_A2A_APP`.
    # `tools/test_mock_aqa.py` pins the real name against these routes.
    @app.get("/a2a/{a2a_app}/.well-known/agent-card.json")
    async def get_a2a_agent_card(
        a2a_app: str, request: Request
    ) -> dict[str, Any]:
        return _build_agent_card(str(request.base_url).rstrip("/"), a2a_app)

    @app.post("/a2a/{a2a_app}")
    async def dispatch_a2a_rpc(a2a_app: str, request: Request) -> Any:
        body = await _read_route_args(request)
        rpc_id = body.get("id")
        method = str(body.get("method") or "")
        raw_params = body.get("params")
        params: dict[str, Any] = (
            raw_params if isinstance(raw_params, dict) else {}
        )

        if method in A2A_STREAMING_METHODS:
            message = params.get("message")
            if not isinstance(message, dict):
                return JSONResponse(
                    _build_rpc_error(
                        rpc_id, -32602, "params.message is required"
                    )
                )
            results = chat.run_turn(message)

            async def stream() -> AsyncIterator[bytes]:
                for index, result in enumerate(results):
                    # Paced rather than dumped: the typing indicator, the
                    # mid-turn cancel and the queue all only exist between
                    # frames, and a stream that arrives at once has no between.
                    if latency_ms and index:
                        await asyncio.sleep(latency_ms / 1000)
                    yield _format_sse_frame(
                        _build_rpc_result(rpc_id, result)
                    ).encode()

            return StreamingResponse(stream(), media_type="text/event-stream")

        if method in A2A_GET_TASK_METHODS:
            task = chat.tasks.get(str(params.get("id") or ""))
            if task is None:
                return JSONResponse(
                    _build_rpc_error(rpc_id, -32001, "Task not found")
                )
            return JSONResponse(_build_rpc_result(rpc_id, task))

        if method in A2A_CANCEL_TASK_METHODS:
            task = chat.cancel(str(params.get("id") or ""))
            if task is None:
                return JSONResponse(
                    _build_rpc_error(rpc_id, -32001, "Task not found")
                )
            return JSONResponse(_build_rpc_result(rpc_id, task))

        # Resubscribe included: nothing calls it, and an error names the gap
        # where an empty stream would read as a turn that never answered.
        return JSONResponse(
            _build_rpc_error(
                rpc_id, -32601, f"{method!r} is not mocked by the AQA chat."
            )
        )

    @app.get("/healthz")
    async def get_liveness() -> dict[str, Any]:
        return {
            "status": "ok",
            "runs": len(data.runs),
            "insights": len(data.insights),
            "generated_at": data.generated_at,
        }

    @app.exception_handler(404)
    def handle_not_found(request: Request, exc: Any) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": f"{request.url.path} is not part of the mocked AQA surface.",
                "served": [
                    "/list-apps",
                    "/investigations/...",
                    "/insights/...",
                    "/documents/...",
                    "/config",
                    "/a2a/<chat app>",
                    "/a2a/<chat app>/.well-known/agent-card.json",
                    "/healthz",
                ],
            },
        )

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve a generated AQA dataset over the agent's command and A2A routes."
    )
    parser.add_argument(
        "--dataset",
        default="scratch/mock_aqa.json",
        help="dataset from tools/mock_aqa_data.py (default: %(default)s).",
    )
    parser.add_argument("--port", type=int, default=8000, help="listen port.")
    parser.add_argument("--host", default="127.0.0.1", help="listen address.")
    parser.add_argument(
        "--app",
        default="mock_aqa",
        help="app name /list-apps reports; local_ui.sh checks it against ADK_APP.",
    )
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=120,
        help="delay between streamed A2A chat frames.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=SETTLE_SECONDS,
        help="how long a scheduled run stays pending before it finishes; "
        "0 leaves it pending forever (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    path = pathlib.Path(args.dataset)
    if not path.exists():
        parser.error(
            f"{path} not found -- generate it first:\n"
            f"  python tools/mock_aqa_data.py --out {path}"
        )
    data = Dataset.load(path, settle_seconds=args.settle_seconds)

    import uvicorn

    age = _render_age_warning(data.generated_at)
    print(
        f"AQA mock on http://{args.host}:{args.port} (app: {args.app})\n"
        f"  {path}: {len(data.runs)} runs, {len(data.insights)} insights, "
        f"{len(data.occurrences)} occurrences{age}\n"
        f"  point the dashboard at it:\n"
        f"    ADK_URL=http://{args.host}:{args.port} ADK_APP={args.app} "
        f"tools/local_ui.sh",
        file=sys.stderr,
    )
    uvicorn.run(
        build_app(data, app_name=args.app, latency_ms=args.latency_ms),
        host=args.host,
        port=args.port,
        log_level="warning",
    )
    return 0


def _render_age_warning(generated_at: str) -> str:
    """Formats an age warning string if the dataset generation timestamp is stale.

    Args:
        generated_at: ISO timestamp when the dataset was generated.

    Returns:
        Warning message string, or empty string if recent or absent.
    """
    if not generated_at:
        return ""
    try:
        hours = (
            dt.datetime.now(tz=dt.UTC) - dt.datetime.fromisoformat(generated_at)
        ).total_seconds() / 3600
    except ValueError:
        return ""
    if hours < 12:
        return ""
    return f"\n  WARNING: generated {hours / 24:.1f} day(s) ago; regenerate for fresh dates"


if __name__ == "__main__":
    raise SystemExit(main())
