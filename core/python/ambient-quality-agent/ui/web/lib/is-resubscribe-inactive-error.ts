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

// Detect the error the @a2a-js/sdk throws when a `tasks/resubscribe` stream
// carries a JSON-RPC error frame. The SDK's `_processSseEventData` does NOT
// yield the error object — it THROWS `new Error("SSE event contained an
// error: <msg> (Code: <n>) Data: ...", { cause: <TypedError> })` (see
// the @a2a-js/sdk client). Our backend yields a terminal frame for two
// recoverable cases:
//   - task not found / no active queue here → TaskNotFound, code -32001
//   - task already terminal → InvalidParams, code -32602
// Both mean "resubscribe can't stream this turn, but tasks/get can fetch the
// finished result", so the client should fall back to getTask rather than
// render a transport error trailer.

// JSON-RPC codes our resubscribe handler uses for recoverable, fall-back-able
// outcomes. Anything else (e.g. InternalError -32603) is a genuine failure.
const RESUBSCRIBE_FALLBACK_CODES = new Set([-32001, -32602]);

// The SDK embeds the code as "(Code: -32001)" in the thrown Error message.
const CODE_IN_MESSAGE_RE = /\(Code:\s*(-?\d+)\)/;

export function isResubscribeInactiveError(err: unknown): boolean {
  if (!err) return false;

  // Preferred signal: the SDK attaches the typed JSON-RPC error as `cause`.
  const cause = (err as { cause?: unknown }).cause;
  const causeName = cause instanceof Error ? cause.name : undefined;
  if (causeName === "TaskNotFoundError") return true;

  // Fallback signal: parse the JSON-RPC code the SDK embeds in the message.
  const message = err instanceof Error ? err.message : String(err);
  const match = CODE_IN_MESSAGE_RE.exec(message);
  if (match) {
    const code = Number(match[1]);
    if (RESUBSCRIBE_FALLBACK_CODES.has(code)) return true;
  }

  return false;
}
