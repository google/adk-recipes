/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * One step in a trajectory: a turn in the observed agent's conversation, or a
 * step the dig phase took while investigating it. Modelled on agents-cli's
 * eval-review `TrajectoryRecord`, trimmed to what our two data sources actually
 * carry.
 *
 * Their record has `startedAt`, `durationMs`, `usage`, `schema`, `thought`
 * and `parentId`/`children` because their telemetry captures timing, token
 * counts and tool schemas. Ours does not: `payload.py` writes only
 * `eval_case_id, turn_index, turn_id, author, event_index, text` for turns
 * and `tool_name, args_json, is_error` for tool calls, and this deployment
 * sets `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: NO_CONTENT`, so a
 * tool's `result` is absent for a specific, known reason rather than merely
 * unfetched. A field with no source is left off the type rather than typed
 * and always undefined, so a reader here cannot expect what neither producer
 * ever supplies.
 */
export type RecordKind = "system" | "user" | "assistant" | "tool";

export interface TrajectoryRecord {
  id: string;
  index: number;
  kind: RecordKind;
  turn: number;
  agent: string;
  text: string;
  parentId?: string;
  toolName?: string;
  args?: unknown;
  result?: unknown;
  isError?: boolean;
  errorMessage?: string;
  raw: unknown;
}

/**
 * `timingRecorded` in the reference is `false` for a whole class of
 * documents so a view can say "no timing" once instead of per-record. Ours
 * has no timing source at all yet either trajectory could carry, so this
 * plays the same role for the fields we may add later without forcing every
 * caller to recompute it from the records.
 */
export interface TrajectoryDoc {
  records: TrajectoryRecord[];
  timingRecorded: false;
}
