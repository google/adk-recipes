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
 * Insight labels are free-form text and can run long; truncating them keeps
 * the opening chat turn readable.
 */
const LABEL_LIMIT = 120;

/**
 * Constructs the initial chat prompt for diagnosing an insight.
 *
 * Formats the prompt with the insight ID and a truncated label to trigger
 * the root-cause analysis (RCA) workflow in chat.
 *
 * @param insightId - Unique identifier of the insight.
 * @param label - Human-readable description or title of the insight.
 * @returns Formatted user prompt for the chat agent.
 */
export function insightRcaPrompt(insightId: string, label: string): string {
  const trimmed = label.trim();
  const short =
    trimmed.length > LABEL_LIMIT
      ? `${trimmed.slice(0, LABEL_LIMIT - 1).trimEnd()}…`
      : trimmed;
  return `Diagnose insight ${insightId} ("${short}") — what is the root cause, and how would you fix it?`;
}
