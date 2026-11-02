// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import type { InvestigationCounters } from "./aqua-api";

export interface CounterField {
  key: keyof InvestigationCounters;
  label: string;
  help: string;
}

export const COUNTER_FIELDS: readonly CounterField[] = [
  {
    key: "traces_scanned",
    label: "Trajectories scanned",
    help: "Everything in the window before sampling -- the denominator for the rest.",
  },
  {
    key: "traces_ingested",
    label: "Trajectories ingested",
    help: "Sampled trajectories that were completely ingested for evaluation.",
  },
  {
    key: "traces_ingested_partial",
    label: "Partially ingested",
    help: "Evaluated, but lost some telemetry on the way in.",
  },
  {
    key: "traces_ingested_failed",
    label: "Ingestion failed",
    help: "Ingestion failed before evaluation: unparseable, or no trajectory content at all.",
  },
  {
    key: "traces_evaluated",
    // Not "evaluated": the funnel's Evaluated total is the trajectories that
    // came back with an outcome, which can be fewer.
    label: "Sent to the eval service",
    help: "Trajectories actually sent to the eval service.",
  },
  {
    key: "traces_eval_passed",
    label: "Passed",
    help: "Every metric on the trajectory passed.",
  },
  {
    key: "traces_eval_failed",
    label: "Failed",
    help: "At least one metric on the trajectory failed.",
  },
  {
    key: "traces_eval_errored",
    label: "Eval service error",
    help: "The eval service crashed or timed out on this trajectory, so it returned no verdict. An AQuA failure, not an agent defect.",
  },
  {
    key: "rubrics_generated",
    label: "Rubrics generated",
    help: "Individual checks evaluated across the ingested trajectories.",
  },
  {
    key: "findings_generated",
    label: "Findings generated",
    help: "Failed findings submitted to clustering, across every producer.",
  },
  {
    key: "rubrics_errored",
    label: "Rubrics errored",
    help: "Failed rubrics whose clustering call raised, so they reached no insight -- findings the investigation looked for and lost.",
  },
  {
    key: "rubrics_unclustered",
    label: "Rubrics unclustered",
    help: "Failed rubrics the clustering result left out: omitted by the model, or in a cluster dropped as ungrounded.",
  },
  {
    key: "clusters_created",
    label: "Clusters created",
    help: "Distinct defect signatures grouped by triage this investigation.",
  },
  {
    key: "clusters_verified",
    label: "Clusters verified",
    help: "Candidates the verification pass confirmed are real defects.",
  },
  {
    key: "clusters_rejected",
    label: "Clusters rejected",
    help: "Candidates judged as non-defects during verification. Insights are still created unless verification enforcement is enabled.",
  },
  {
    key: "clusters_verify_skipped",
    label: "Clusters not verified",
    help: "Candidates the verification pass did not judge, for want of a trajectory or past its per-investigation cap.",
  },
  {
    key: "clusters_verify_failed",
    label: "Verification failures",
    help: "Candidates whose verification call failed. They create no insight.",
  },
  {
    key: "insights_created",
    label: "New insights",
    help: "First time this defect was observed across any investigation.",
  },
  {
    key: "insights_recurring",
    label: "Recurring insights",
    help: "Defect matched a previously recorded insight.",
  },
];
