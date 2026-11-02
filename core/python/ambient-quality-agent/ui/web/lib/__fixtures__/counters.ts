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

import type { InvestigationCounters } from "@/lib/aqua-api";

/**
 * Baseline zero-value counter fixture. Explicit typing ensures compile-time
 * validation when new fields are added to `InvestigationCounters`.
 */
const ZERO_COUNTERS: InvestigationCounters = {
  traces_scanned: 0,
  traces_ingested: 0,
  traces_ingested_partial: 0,
  traces_ingested_failed: 0,
  traces_evaluated: 0,
  traces_eval_passed: 0,
  traces_eval_failed: 0,
  traces_eval_errored: 0,
  rubrics_generated: 0,
  findings_generated: 0,
  rubrics_errored: 0,
  rubrics_unclustered: 0,
  clusters_created: 0,
  clusters_verified: 0,
  clusters_rejected: 0,
  clusters_verify_skipped: 0,
  clusters_verify_failed: 0,
  insights_created: 0,
  insights_recurring: 0,
};

/**
 * Creates an `InvestigationCounters` fixture with default zero values and
 * optional overrides.
 *
 * @param overrides - Partial counter values to merge with default zero counters.
 * @returns A complete `InvestigationCounters` object.
 */
export function counters(
  overrides: Partial<InvestigationCounters> = {},
): InvestigationCounters {
  return { ...ZERO_COUNTERS, ...overrides };
}
