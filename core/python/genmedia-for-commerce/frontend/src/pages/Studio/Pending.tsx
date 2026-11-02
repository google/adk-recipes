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

// The variations a running job has still to make, one card each, and how many failed.
import { SpinnerIcon } from "../../components/icons";

/** A card the size of a result's, for one variation still coming. */
export function PendingCard({ ratio }: { ratio: string }) {
  return (
    <div
      role="status"
      aria-label="Generating…"
      className="glass-panel rounded-2xl overflow-hidden flex flex-col h-full animate-pulse-soft"
    >
      <div className="bg-black/40" style={{ aspectRatio: ratio }} />
      <p className="p-4 flex items-center gap-1.5 text-sm text-gm-text-secondary">
        <SpinnerIcon size={12} className="text-gm-accent" />
        Generating…
      </p>
    </div>
  );
}

/** "1 video failed", "2 images failed": the variations of a list that ended without media. */
export function FailedNote({ count, noun }: { count: number; noun: string }) {
  if (count <= 0) return null;
  return (
    <span className="chip bg-gm-warning/10 text-gm-warning ring-1 ring-inset ring-gm-warning/25">
      {`${count} ${noun}${count === 1 ? "" : "s"} failed`}
    </span>
  );
}
