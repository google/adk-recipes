/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/** The evaluator's score as a coloured badge: green from 80, amber from 60, red below. */
export default function ScoreBadge({ score }: { score: number }) {
  const n = Math.round(score);
  const tone =
    n >= 80
      ? "text-gm-success bg-gm-success/10 ring-gm-success/25"
      : n >= 60
        ? "text-gm-warning bg-gm-warning/10 ring-gm-warning/25"
        : "text-gm-error bg-gm-error/10 ring-gm-error/25";
  return (
    <span
      title="Evaluator score, 0-100"
      className={`chip ring-1 ring-inset ${tone}`}
    >
      Score {n}
    </span>
  );
}
