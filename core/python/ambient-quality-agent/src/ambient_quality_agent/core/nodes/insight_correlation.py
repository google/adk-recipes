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

"""The ``insight_correlation`` node

Wiring::

    insight_correlation_node ── delete_failed_insights() ──► InsightWriter (Protocol)
        │ merge_finding_sets() ◄── state["finding_sets"]        ▲ implements
        ▼                                                   BigQueryInsightStore,
    clustering.cluster_and_label() ◄── model_call_factory()  InMemoryInsightStore
        │ validate_clusters(), attach_examples()
        ▼
    merge.merge_clusters() ◄── merge_call_factory()
        │
        ▼
    _verify() ──► verification.verify_clusters() ◄── verification_call_factory()
        │ _Verdicts.to_match, _build_match_candidate()
        ▼
    InsightWriter.find_existing_insights()
        │
        ▼
    _build_records_to_save() ──► InsightWriter.save_investigation_result()
        │
        ▼
    InsightWriter.resolve_stale_insights(), skipped on custom-triggered runs

Clusters this sweep's failed rubrics into labelled candidate issues, judges them
against their own traces unless ``insights_verification_enabled`` turns the pass
off, and even then only the largest `MAX_VERIFIED_CLUSTERS` are judged -- matches everything the judge did
not reject against the insights earlier sweeps recorded for the same agent, and
writes one occurrence per candidate. A recurring defect is then tracked as one
insight with history rather than resurfacing as a fresh finding every run.

flow: failed rubrics -> candidate clusters -> one occurrence per candidate, and
then, on the verdict that candidate got:

  - tracked and matched: that insight is marked recurring;
  - tracked and unmatched: a new insight is minted;
  - unjudged, the pass having skipped the candidate or failed on it: nothing is
    minted and the occurrence names no insight, but a matched insight is dated
    to this sweep so it does not age out unseen;
  - rejected: never matched, nothing minted, the occurrence names no insight.

A negative verdict only rejects under ``insights_verification_enforced``.
Unenforced, the candidate stays tracked with the verdict attached, so a trace
too thin to prove the defect leaves a triager the judge's reading rather than
silence.

With the pass off no candidate is judged and none is withheld: every one takes
one of the first two branches, as they all did before the pass existed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from ambient_quality_agent import config as config_module
from ambient_quality_agent.core._logging import log_node_run
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.state import WorkflowState, add_counts
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    resolve_latest_revision,
)
from ambient_quality_agent.tools.insights import clustering, merge, verification
from ambient_quality_agent.tools.insights.clustering import (
    Cluster,
    ClusteringInfo,
)
from ambient_quality_agent.tools.insights.findings import (
    Finding,
    merge_finding_sets,
)
from ambient_quality_agent.tools.insights.models import (
    ClusterVerification,
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
    mint_id,
)
from ambient_quality_agent.tools.insights.verification import VerifyInfo
from ambient_quality_agent.tools.investigations.models import (
    CUSTOM_TRIGGER_TYPE,
)
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.sessions.state import State
from google.adk.workflow import node

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _Verdict:
    """What the verification pass settled on for one candidate cluster.

    `state` is the decision and the only thing downstream reads: the pass
    tracked the candidate, threw it out, or reached no verdict on it. A pass
    that did not run tracks every candidate, so no reader of this has to know
    whether it ran -- nothing was withheld from a check nobody made.
    """

    state: OccurrenceState

    analyses: dict[str, ClusterVerification] = dataclasses.field(
        default_factory=dict
    )
    """The verdict as `InsightOccurrence.analyses` carries it, empty unless the
    pass reached one."""

    examined_case_ids: frozenset[str] = frozenset()
    """The cases the pass was shown, which its occurrence marks ``examined``."""

    @property
    def flagged(self) -> bool:
        """Whether the judge came back negative on this candidate.

        Not `state`: unenforced, a negative verdict leaves the candidate
        tracked, and this is all that distinguishes it from a clean one.
        """
        record = self.analyses.get(verification.ANALYSIS_STEP)
        return record is not None and not record.valid


@dataclasses.dataclass(frozen=True)
class _Verdicts:
    """The verification pass's whole answer for one sweep."""

    per_cluster: list[_Verdict]
    """One verdict per candidate, indexed by cluster index."""

    tally: VerifyInfo
    """The outcome counts, all zero when the pass did not run."""

    @property
    def to_match(self) -> list[int]:
        """Indices of the clusters to put to the matching judge, in order.

        Everything but a rejection. A rejected candidate tracks no insight, so
        asking the judge which one it resurfaces is a question with no consumer;
        an unjudged one still has to find the insight it belongs to, in order to
        keep that insight alive.
        """
        return [
            index
            for index, verdict in enumerate(self.per_cluster)
            if verdict.state is not OccurrenceState.REJECTED
        ]


_Candidate = tuple[Cluster, str | None, _Verdict]
"""One cluster, the insight it matched (``None`` when it matched none, and
always for a rejected cluster, which is never matched at all), and what the
verification pass settled on for it."""


@node(name="insight_correlation")
@log_node_run("insight_correlation")
def insight_correlation_node(ctx: Context) -> Event:
    """Correlate this sweep's failed rubrics into deduplicated insights and store them in a db."""
    state = ctx.state
    # This node runs inside the durable investigation LRO, whose only window is
    # Cloud Logging -- the record_progress_event goes to the chat stream, not the logs.
    # So log each milestone at INFO (once per sweep, not noisy) so a deployed run
    # is observable without reading BigQuery.
    run_id = state.get("run_id", "")
    auto_resolve_days = state.get("insights_auto_resolve_days", 0)
    trigger_type = state.get("trigger_type", "")
    verification_enabled = config_module.is_verification_enabled(
        state.get("insights_verification_enabled")
    )
    verification_enforced = config_module.is_verification_enforced(
        state.get("insights_verification_enforced")
    )
    store = _common.insight_store_factory(ctx)
    now = dt.datetime.now(dt.UTC)

    # Erase any prior attempt of this run before anything else, so a crash-retry
    # cannot leave the earlier attempt's insights orphaned or double-counted.
    store.delete_failed_insights(run_id)

    # Merge findings across all sweep scopes to establish stable finding IDs.
    raw_sets = state.get("finding_sets") or []
    finding_set = merge_finding_sets(raw_sets)
    findings = finding_set.findings
    logger.info(
        "insight_correlation: run_id=%s cleaned prior attempt; merged %d finding "
        "set(s) into %d failed rubric(s) and the agent configuration(s) of "
        "%d session(s).",
        run_id,
        len(raw_sets),
        len(findings),
        len(finding_set.agent_revisions),
    )
    if findings:
        clusters, rubrics_skipped_to_errors = clustering.cluster_and_label(
            findings, model_call=_common.model_call_factory(ctx)
        )
        clusters = clustering.validate_clusters(clusters, len(findings))
        clustered = len({fid for c in clusters for fid in c.finding_ids})
        clustering_info = ClusteringInfo(
            errored_rubrics=rubrics_skipped_to_errors,
            unclustered_rubrics=len(findings)
            - rubrics_skipped_to_errors
            - clustered,
        )
        clusters = clustering.attach_examples(
            clusters, findings, finding_set.sessions
        )
        # After the evidence, so a folded candidate keeps a real trace, and
        # before the store, which matches candidates against saved insights but
        # never against each other -- two chunks' wordings of one defect would
        # otherwise mint an insight each.
        clusters = merge.merge_clusters(
            clusters, model_call=_common.merge_call_factory(ctx)
        )
        verdicts = _verify(
            clusters,
            ctx,
            agent_definitions=finding_set.agent_revisions,
            enabled=verification_enabled,
            enforced=verification_enforced,
        )
        # `find_existing_insights` keys its answer by position in the list it was
        # given, so the candidate's own index has to be read back out of
        # `to_match` -- a sighting filed under the wrong cluster would attach to
        # someone else's insight.
        to_match = verdicts.to_match
        matches = store.find_existing_insights(
            [
                _build_match_candidate(
                    clusters[index], verdicts.per_cluster[index]
                )
                for index in to_match
            ]
        )
        matched_by_cluster = {
            to_match[position]: insight_id
            for position, insight_id in matches.items()
        }
        candidates: list[_Candidate] = [
            (
                cluster,
                matched_by_cluster.get(index),
                verdicts.per_cluster[index],
            )
            for index, cluster in enumerate(clusters)
        ]
        new_insights, seen_ids, recurring_ids, relabelled, occurrences = (
            _build_records_to_save(candidates, state, now)
        )
        store.save_investigation_result(
            new_insights,
            seen_ids,
            recurring_ids,
            occurrences,
            now,
            relabelled=relabelled,
        )
        add_counts(
            state,
            clusters_created=len(clusters),
            clusters_verified=verdicts.tally.verified,
            clusters_rejected=verdicts.tally.rejected,
            clusters_verify_skipped=verdicts.tally.skipped,
            clusters_verify_failed=verdicts.tally.failed,
            insights_created=len(new_insights),
            insights_recurring=len(recurring_ids),
            rubrics_errored=clustering_info.errored_rubrics,
            rubrics_unclustered=clustering_info.unclustered_rubrics,
        )
        text = _render_correlation_summary(
            findings,
            candidates,
            clustering_info,
            verdicts.tally,
            enforced=verification_enforced,
        )
        logger.info(
            "insight_correlation: run_id=%s persisted %d cluster(s) as %d new "
            "insight(s) + %d recurring; %d flagged by the judge and %d "
            "unjudged; %d occurrence(s).",
            run_id,
            len(clusters),
            len(new_insights),
            len(recurring_ids),
            verdicts.tally.rejected,
            verdicts.tally.skipped + verdicts.tally.failed,
            len(occurrences),
        )
    else:
        text = "**Insights:** no failed rubrics to correlate.\n"
        logger.info(
            "insight_correlation: run_id=%s no failed rubrics; nothing to persist.",
            run_id,
        )

    # Auto-resolution runs on ambient sweeps to retire stale insights based on
    # continuous observation over time. Custom sweeps target user-selected
    # sessions and skip auto-resolution to avoid retiring unexamined insights.
    #
    # Custom sweeps still record sightings for matched telemetry, which is
    # mostly right: their selector ran against real production telemetry. The
    # residual wrong case is a selector naming an old window, refreshing recency
    # as though the defect were seen today. Bounded, and visible on the run
    # record; the fix if it bites is a `last_ambient_seen_at` column that only
    # ambient sweeps advance.
    if trigger_type == CUSTOM_TRIGGER_TYPE:
        logger.info(
            "insight_correlation: run_id=%s custom run; skipped auto-resolution.",
            run_id,
        )
    else:
        store.resolve_stale_insights(now, auto_resolve_days)
        logger.info(
            "insight_correlation: run_id=%s resolved insights stale beyond %s day(s).",
            run_id,
            auto_resolve_days,
        )

    return WorkflowState.record_progress_event(
        state, text, "insight_correlation"
    )


def _verify(
    clusters: list[Cluster],
    ctx: Context,
    *,
    agent_definitions: dict[str, list[AgentRevision]],
    enabled: bool,
    enforced: bool,
) -> _Verdicts:
    """Judge this sweep's candidates and say what each one is worth.

    Switched off, this makes no model call at all -- it is the sweep's most
    expensive step -- and tracks every candidate, so the sweep mints and matches
    exactly as it did before the pass existed. Its empty tally is what keeps the
    summary line and the four counters from reporting a pass that never ran.

    `enforced` decides what a negative verdict costs. Enforced, the candidate is
    `REJECTED` and withheld from minting and matching; unenforced it stays
    `TRACKED`, keeping the label and explanation the judge wrote and carrying
    the verdict for a triager to weigh.

    Every candidate comes back with a verdict either way, so `enabled` stops
    here: nothing downstream has to tell a pass that withheld nothing from one
    that was never asked.
    """
    if not enabled:
        return _Verdicts(
            [_Verdict(OccurrenceState.TRACKED) for _ in clusters], VerifyInfo()
        )

    try:
        outcomes, tally = verification.verify_clusters(
            clusters,
            model_call=_common.verification_call_factory(ctx),
            agent_definitions=agent_definitions,
        )
    except Exception:
        # `verify_clusters` raises when every call it made failed, which in a
        # sweep holding one judgeable cluster is one transient error. Letting
        # that escape discards the clustering and merge spend and writes no
        # audit row at all, when UNJUDGED already means what happened.
        logger.exception("insight_correlation: the verification pass failed.")
        return _Verdicts(
            [_Verdict(OccurrenceState.UNJUDGED) for _ in clusters],
            VerifyInfo(failed=len(clusters)),
        )
    per_cluster: list[_Verdict] = []
    for index in range(len(clusters)):
        outcome = outcomes.get(index)
        if outcome is None:
            # The pass skipped this candidate or failed on it. Absence is no
            # verdict rather than a negative one, and it is the occurrence's
            # state because an unchecked finding a reader cannot tell from a
            # verified one is what the pass exists to prevent.
            per_cluster.append(_Verdict(OccurrenceState.UNJUDGED))
            continue
        state = (
            OccurrenceState.REJECTED
            if outcome.rejected and enforced
            else OccurrenceState.TRACKED
        )
        per_cluster.append(
            _Verdict(
                state,
                analyses={verification.ANALYSIS_STEP: outcome.verification},
                examined_case_ids=frozenset(outcome.examined_case_ids),
            )
        )
    return _Verdicts(per_cluster, tally)


def _render_correlation_summary(
    findings: list[Finding],
    candidates: list[_Candidate],
    clustering_info: ClusteringInfo,
    verify_info: VerifyInfo,
    *,
    enforced: bool,
) -> str:
    """Format the correlation result as human-readable markdown for the run log.

    Summarizes the grouping of findings into candidate clusters, recurrence
    matches against existing insights, and cluster status breakdown.
    """
    statuses: list[str] = []
    lines = []
    for cluster, matched_id, verdict in candidates:
        # `new` and `recurring` are what the header counts as insights, so a
        # candidate that minted none must not land in either.
        if verdict.state is OccurrenceState.UNJUDGED:
            status = "unjudged"
        elif verdict.state is OccurrenceState.REJECTED:
            status = "rejected"
        else:
            status = "recurring" if matched_id else "new"
        statuses.append(status)
        # Distinguish flagged-but-tracked candidates in the log line without
        # altering status counts in the header summary.
        shown = (
            f"{status}, flagged"
            if verdict.flagged and verdict.state is OccurrenceState.TRACKED
            else status
        )
        cases = [e.eval_case_id for e in cluster.examples if e.eval_case_id]
        cases_text = ", ".join(cases) if cases else "no case ids"
        lines.append(
            f"- _{cluster.label}_ ({shown}): {cluster.item_count} rubric(s) "
            f"across {cluster.trace_count} trace(s); cases: {cases_text}\n"
        )
    header = (
        f"**Insights:** {len(findings)} failed rubric(s) grouped into "
        f"{len(candidates)} candidate issue(s): {statuses.count('new')} new, "
        f"{statuses.count('recurring')} recurring.\n"
    )
    return (
        header
        + _render_skipped_rubrics_line(clustering_info, len(findings))
        + _render_verification_line(verify_info, enforced=enforced)
        + "".join(lines)
    )


def _render_skipped_rubrics_line(
    clustering_info: ClusteringInfo, total: int
) -> str:
    """One line for the rubrics that produced no insight, omitted when none did."""
    if not (
        clustering_info.errored_rubrics or clustering_info.unclustered_rubrics
    ):
        return ""
    return (
        f"**Insights -- skipped rubric(s):** {clustering_info.errored_rubrics} errored, "
        f"{clustering_info.unclustered_rubrics} unclustered (of {total} total); "
        f"see the logs for details.\n"
    )


def _render_verification_line(
    verify_info: VerifyInfo, *, enforced: bool
) -> str:
    """One line for the candidates the pass did not confirm, omitted when it
    confirmed them all.

    Without it a sweep that confirmed nothing reads as a clean run: the header
    counts no new issue and no recurring one, and nothing says that the pass,
    rather than an absence of defects, is why. Enforced, it is also where the
    header's arithmetic closes -- the four counts sum to the candidates, of
    which only the verified are the new and recurring ones counted above; that
    is why the line says which way a negative verdict was taken.

    A pass that did not run leaves the same four zeros and is omitted for the
    same reason: it withheld nothing, and a sweep that never verified must not
    read as one that verified nothing.

    Unenforced the same count is named flagged, because the candidate behind it
    is one the header already counted as new or recurring: calling it rejected
    reads as a candidate both thrown out and minted.
    """
    if not (verify_info.rejected or verify_info.skipped or verify_info.failed):
        return ""
    negative, verdict_use = (
        ("rejected", "only a verified candidate becomes an insight")
        if enforced
        else ("flagged", "a flagged candidate is recorded and kept")
    )
    return (
        f"**Insights -- verification:** {verify_info.verified} verified, "
        f"{verify_info.rejected} {negative}, {verify_info.skipped} skipped, "
        f"{verify_info.failed} failed; {verdict_use}.\n"
    )


def _resolve_best_label(cluster: Cluster, verdict: _Verdict) -> str:
    """The label to store: the verdict's rename when it proposed one.

    Clustering names a candidate from the finding tuples alone. Verification
    sees the trajectories and the agent's configuration, so its rename is the
    better description of the same defect, and it is what a reader and the
    recurrence judge should both be given.
    """
    verdict_record = verdict.analyses.get(verification.ANALYSIS_STEP)
    refined = verdict_record.refined_label if verdict_record else ""
    return refined or cluster.label


def _build_match_candidate(cluster: Cluster, verdict: _Verdict) -> Cluster:
    """The candidate as the recurrence judge should see it.

    An insight stores `_resolve_best_label`, so that is what the judge must be given for
    the candidate too. Matching the raw clustering label against a stored
    refined one compares two different kinds of description -- one names what
    the sweep observed, the other what verification concluded after reading the
    trajectory -- and a recurrence goes unrecognised.

    Only the label differs. Everything else on the cluster is the sweep's own,
    and the occurrence keeps the observed label.
    """
    return cluster.model_copy(
        update={"label": _resolve_best_label(cluster, verdict)}
    )


def _build_records_to_save(
    candidates: list[_Candidate],
    state: State,
    now: dt.datetime,
) -> tuple[
    list[Insight], list[str], list[str], dict[str, str], list[InsightOccurrence]
]:
    """Turn this sweep's candidates into the rows `InsightStore` persists.

    Every candidate yields exactly one occurrence, carrying whatever verdict the
    verification pass reached. A tracked candidate appends to the insight it
    matched, or mints a fresh ``NEW`` one. A candidate the pass rejected or
    never judged mints nothing and names no insight: its occurrence is the
    audit trail of the sighting alone.

    The two part company over the insight each matched. A rejection leaves it
    unseen, so `InsightStore.resolve_stale_insights` ages it out, which is the
    intent. An unjudged candidate reports its match as seen, dating the insight
    without claiming it recurred: resolution is terminal, and an absent verdict
    is no evidence the defect is gone.

    Returns the insights to mint, the ids this sweep saw, the subset a verdict
    confirmed recurring, the renames a verdict proposed for insights already
    stored, and the occurrences to write.
    """
    agent_name = state["observed_agent_name"]
    run_id = state.get("run_id", "")

    new_insights: list[Insight] = []
    seen_ids: list[str] = []
    recurring_ids: list[str] = []
    relabelled: dict[str, str] = {}
    occurrences: list[InsightOccurrence] = []
    for cluster, matched_id, verdict in candidates:
        insight_id = matched_id
        if verdict.state is OccurrenceState.UNJUDGED:
            # A rejection is evidence the defect is not real, so letting the
            # insight age out is right. An absent verdict is no such evidence,
            # so only this branch keeps the insight it matched alive.
            insight_id = None
            if matched_id:
                seen_ids.append(matched_id)
        elif verdict.state is OccurrenceState.REJECTED:
            insight_id = None
        elif matched_id:
            seen_ids.append(matched_id)
            recurring_ids.append(matched_id)
            # The stored label is whatever clustering called this defect the
            # first time it was seen. A verdict that read the traces names it
            # better, and that name is what the next sweep matches against.
            best = _resolve_best_label(cluster, verdict)
            if best != cluster.label:
                relabelled[matched_id] = best
        else:
            insight_id = mint_id()
            new_insights.append(
                Insight(
                    insight_id=insight_id,
                    agent_name=agent_name,
                    label=_resolve_best_label(cluster, verdict),
                    status=InsightStatus.NEW,
                    created_at=now,
                    updated_at=now,
                )
            )
        occurrences.append(
            InsightOccurrence(
                occurrence_id=mint_id(),
                insight_id=insight_id,
                occurrence_state=verdict.state,
                run_id=run_id,
                created_at=now,
                agent_name=agent_name,
                agent_revision=resolve_latest_revision(cluster.agent_revisions),
                label=cluster.label,
                item_count=cluster.item_count,
                trace_count=cluster.trace_count,
                analyses=verdict.analyses,
                # `Cluster.trace_ids` already holds trajectory ids, and
                # `trace_count` is their number; both are written so a row
                # stays readable next to the ones written before the ids were.
                trajectory_ids=cluster.trace_ids,
                rubrics=[
                    example.model_copy(update={"examined": True})
                    if example.eval_case_id in verdict.examined_case_ids
                    else example
                    for example in cluster.examples
                ],
            )
        )
    return new_insights, seen_ids, recurring_ids, relabelled, occurrences
