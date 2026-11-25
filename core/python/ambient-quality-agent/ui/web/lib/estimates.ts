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

/**
 * Figures for all traffic, as opposed to figures about the sample.
 *
 * Investigations may review risky conversations more often than others, and
 * record each conversation's chance of being picked, `p`. A figure about all
 * traffic counts each reviewed conversation as 1 / p conversations; the engine
 * sums those into the `estimated_*` counters and into each insight's
 * `estimated_share`. The raw counts describe the sample.
 */

import type { InvestigationCounters, SamplingSignals } from "./aqua-api";
import { count } from "./funnel";

/** Why an estimated rate differs from the sample's, for help beside one. */
export const ESTIMATE_HELP =
  "Estimated for every conversation the investigation picked from: its window, or what its selector matched. Each reviewed conversation counts for the conversations it stands for, so reviewing risky conversations more often does not skew the rate.";

/** How an insight's share is estimated, for help beside one. */
export const SHARE_HELP =
  "Estimated share of all conversations, over the investigations from the first that found this issue to the latest. Investigations that ran a selector are left out, since they reviewed a chosen slice rather than all traffic.";

/** Whether an investigation recorded estimates for all traffic
 *  (`InvestigationCounters.has_estimates` on the engine).
 *
 * @param counters - The investigation's counters, possibly partial.
 * @returns True when the `estimated_*` counters hold estimates.
 */
export function hasEstimates(
  counters: Partial<InvestigationCounters> | undefined,
): boolean {
  return (
    count(counters?.estimated_trajectories_eval_passed) +
      count(counters?.estimated_trajectories_eval_failed) >
    0
  );
}

/** A share of all conversations as a percent a reader can take in at a
 *  glance: "<0.1%" below a tenth of a percent, one decimal below 10%, whole
 *  percents above, and never above 100%.
 *
 * @param share - The share, from 0 to 1; overlapping occurrences can push an
 *   insight's estimate above 1, which reads as 100%.
 * @returns The percent, such as "1.2%".
 */
export function formatShare(share: number): string {
  const percent = Math.min(share, 1) * 100;
  if (percent > 0 && percent < 0.1) return "<0.1%";
  if (percent < 10) return `${percent.toFixed(1).replace(/\.0$/, "")}%`;
  return `${Math.round(percent)}%`;
}

/** How widespread an insight is: "about 1.2% of conversations · 715
 *  reviewed", or "715 trajectories" without an estimate. No percentage
 *  without one, since a bare 0% would read as "certainly nothing" rather than
 *  "not measured".
 *
 * @param reviewed - The insight's `trajectory_count`, already formatted.
 * @param share - Its `estimated_share`.
 * @param trajectories - `trajectory_count` as a number, for the plural.
 * @returns The phrase.
 */
export function formatInsightReach(
  reviewed: string,
  share: number | null | undefined,
  trajectories: number,
): string {
  if (share == null) {
    return `${reviewed} ${trajectories === 1 ? "trajectory" : "trajectories"}`;
  }
  return `about ${formatShare(share)} of conversations · ${reviewed} reviewed`;
}

/** Where among the window's trajectories a percentile puts one, such as
 *  "top 5%", or the window's highest for a percentile of 1, which "top 1%"
 *  would misstate in a small window.
 *
 * @param percentile - The percentile, from 0 to 1.
 * @param highest - The phrase for the window's highest, such as "slowest in
 *     window".
 * @returns The phrase, never below "top 1%".
 */
function formatRank(percentile: number, highest: string): string {
  if (percentile >= 1) return highest;
  return `top ${Math.max(1, Math.round(100 * (1 - percentile)))}%`;
}

/** One risk signal that raised a trajectory's sampling weight. */
export interface PickedSignal {
  /** Stable id, one per signal. */
  key:
    | "failed_call"
    | "cut_off_response"
    | "tool_reported_failure"
    | "repeated_tool_calls"
    | "high_token_usage"
    | "slow_turn";
  /** Short label, such as "Failed call". */
  label: string;
  /** What the signal means, for a tooltip. */
  description: string;
  /** Where the trajectory ranks in the window, such as "top 5%"; only for
   *  signals that rank a measure. */
  rank?: string;
}

/** The ranked part of a tail signal that raised a weight, or null when it
 *  raised none. A tail above 0 always has a percentile.
 *
 * @param tail - The signal's tail score.
 * @param percentile - The measure's percentile.
 * @param highest - The phrase for the window's highest.
 * @returns The rank, or null.
 */
function resolveTailRank(
  tail: number,
  percentile: number | null | undefined,
  highest: string,
): string | null {
  if (!(tail > 0) || percentile == null) return null;
  return formatRank(percentile, highest);
}

/** The signals that raised a trajectory's sampling weight, each with a label
 *  a reader understands without knowing the sampler, such as "Failed call" or
 *  "High token usage" (top 5%). Empty for a trajectory picked at the base
 *  weight, or under uniform sampling.
 *
 * @param signals - The trajectory's `sampling_signals`.
 * @returns The signals, strongest first.
 */
export function listPickedFor(
  signals: SamplingSignals | null | undefined,
): PickedSignal[] {
  if (!signals) return [];
  const picked: PickedSignal[] = [];
  if (signals.has_error) {
    picked.push({
      key: "failed_call",
      label: "Failed call",
      description:
        "A model, tool or agent call failed with an error: it raised, or its span has an error status.",
    });
  }
  if (signals.has_abnormal_finish) {
    picked.push({
      key: "cut_off_response",
      label: "Cut-off response",
      description:
        "A model response stopped before finishing: it hit the token limit, was blocked for safety, or produced a malformed function call.",
    });
  }
  if (signals.has_tool_reported_error) {
    picked.push({
      key: "tool_reported_failure",
      label: "Tool reported failure",
      description:
        "A tool ran, but its result reports a failure, such as an error field or success: false.",
    });
  }
  const tails = [
    {
      key: "repeated_tool_calls",
      label: "Repeated tool calls",
      description:
        "The most calls to one tool in one turn, ranked among the window's conversations.",
      rank: resolveTailRank(
        signals.busy_turn_tail,
        signals.busy_turn_percentile,
        "most in window",
      ),
    },
    {
      key: "high_token_usage",
      label: "High token usage",
      description:
        "Total tokens across the conversation, ranked among the window's conversations.",
      rank: resolveTailRank(
        signals.cost_tail,
        signals.cost_percentile,
        "most in window",
      ),
    },
    {
      key: "slow_turn",
      label: "Slow turn",
      description:
        "Duration of the conversation's slowest turn, ranked among the window's conversations.",
      rank: resolveTailRank(
        signals.latency_tail,
        signals.latency_percentile,
        "slowest in window",
      ),
    },
  ] as const;
  for (const { key, label, description, rank } of tails) {
    if (rank != null) picked.push({ key, label, description, rank });
  }
  return picked;
}

/** One signal as text, such as "High token usage (top 5%)", for a tooltip or
 *  a screen reader.
 *
 * @param signal - The signal.
 * @returns The label, with its rank when it has one.
 */
export function formatPickedSignal(signal: PickedSignal): string {
  return signal.rank ? `${signal.label} (${signal.rank})` : signal.label;
}
