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

/** Telling "nothing has been exported yet" apart from "something is broken".
 *
 * A deployment with no telemetry does not fail quietly. The queries a sweep
 * runs are written against the columns the logging sink produces, and BigQuery
 * infers those columns from the rows it holds: until a trace carrying a label
 * lands, the column does not exist and every read of it is a 400. The dataset
 * itself does not exist until the sink first writes to it, so the reads before
 * that are 404s. Both arrive as raw BigQuery text, and rendering them as
 * failures paints every pane red on the one deployment whose owner has no way
 * to tell an empty product from a broken one.
 */

import { HttpError, ReadTimeoutError } from "./aqua-api";
import { isTransportError } from "./is-transport-error";

/** A column the query names that the export table has not grown yet. The
 *  capture group is the column, so the note can say which signal is missing;
 *  BigQuery reports it against the sink's flattened `labels` STRUCT. */
const MISSING_COLUMN_RE = /field name (\w+) does not exist in struct</i;

/** The dataset or table the sink creates on its first write. Covers the raw
 *  BigQuery text and the `NotFound: 404 …` form `ui/app.py` wraps a
 *  `google.api_core` exception in. */
const MISSING_TABLE_RE =
  /not found: (?:table|dataset)\b|was not found in location/i;

/** An engine with no agent on it yet: Terraform's placeholder image, or a
 *  revision mid-rollout. `ui/app.py` words the whole message and
 *  `tests/test_ui_investigations.py` pins it against this pattern. */
const NOT_DEPLOYED_RE = /is not serving its API yet/i;

export type FailureKind =
  /** Nothing has been recorded yet. A state, not a fault. */
  | "no-data"
  /** The deployment is unfinished. A state, and one that clears itself. */
  | "pending"
  /** A real failure: unreachable, timed out, or a fault worth reporting. */
  | "error";

export interface Failure {
  kind: FailureKind;
  /** What happened, as a phrase that follows a caller-supplied lead-in — so
   *  the same classification reads correctly under "Couldn't load the totals"
   *  and under "This investigation stopped early". Ends in a full stop. */
  cause: string;
  /** Why the state is expected and what ends it. Never on an `error`. */
  hint?: string;
  /** The underlying text, for a disclosure. Empty when `cause` is already it. */
  detail: string;
}

/** The message a thrown read carries, however it was thrown. */
function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error ?? "");
}

/** Classifies a failed read or a failed run. */
export function describeFailure(error: unknown): Failure {
  const detail = messageOf(error);

  const missingColumn = MISSING_COLUMN_RE.exec(detail);
  if (missingColumn) {
    return {
      kind: "no-data",
      cause: "No telemetry to analyze yet.",
      hint:
        `The exported trajectories carry no "${missingColumn[1]}" field, so there is` +
        " nothing here to read. It appears once the observed agent has served" +
        " traffic and the logging sink has exported it.",
      detail,
    };
  }

  if (MISSING_TABLE_RE.test(detail)) {
    return {
      kind: "no-data",
      cause: "No telemetry to analyze yet.",
      hint:
        "The BigQuery table the logging sink writes to does not exist. The" +
        " first export creates it, so this clears once the observed agent has" +
        " served traffic.",
      detail,
    };
  }

  if (NOT_DEPLOYED_RE.test(detail)) {
    // `ui/app.py` opens with the state and follows it with the why, so the
    // first sentence is the line every pane can show and the rest is the hint
    // the roomy ones add. Split rather than restated, to keep one wording.
    const stop = detail.indexOf(". ");
    return {
      kind: "pending",
      cause: stop < 0 ? detail : detail.slice(0, stop + 1),
      hint: stop < 0 ? undefined : detail.slice(stop + 2),
      detail: "",
    };
  }

  if (error instanceof ReadTimeoutError) {
    return {
      kind: "error",
      cause: "the agent didn't respond in time.",
      detail,
    };
  }
  if (error instanceof HttpError || isTransportError(error)) {
    return { kind: "error", cause: "couldn't reach the agent.", detail };
  }

  return {
    kind: "error",
    cause: detail || "no reason given.",
    // `cause` already carries it; a disclosure repeating it adds nothing.
    detail: "",
  };
}

/** Whether a failure is really an empty deployment.
 *
 * For the callers that need only the tone — a status chip, a row flag — and
 * have no room for a sentence.
 */
export function isNoData(error: unknown): boolean {
  const message = messageOf(error);
  return MISSING_COLUMN_RE.test(message) || MISSING_TABLE_RE.test(message);
}
