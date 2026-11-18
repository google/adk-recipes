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
 * Browser-side latency tracker for non-blocking voice tools (`app/voice/non_blocking.py`).
 * ADK sends the real function response only to the model, so the browser tracks when
 * the context card lands on `/state` and when the first model audio starts after kickoff.
 */

import type { A2UISurface } from "./types";

export interface LatencyPhases {
  result?: number;
  card?: number;
  audio?: number;
}

export const LATENCY_TIMEOUT_MS = 30_000;
export const CARD_WAIT_MS = 15_000;

const PHASE_LABELS: [keyof LatencyPhases, string][] = [
  ["result", "result"],
  ["card", "card"],
  ["audio", "first audio"],
];

export function formatLatency(name: string, phases: LatencyPhases): string {
  const seen = PHASE_LABELS.filter(([key]) => phases[key] !== undefined).map(
    ([key, label]) => `${label} +${((phases[key] ?? 0) / 1000).toFixed(1)}s`,
  );
  return `[latency] ${name} ${seen.length ? seen.join(" · ") : "nothing seen"}`;
}

interface Call {
  key: string;
  name: string;
  start: number;
  slow: boolean;
  kickoffDone: boolean;
  phases: LatencyPhases;
}

export interface ToolPart {
  id?: string;
  name: string;
  response?: { status?: unknown; [field: string]: unknown };
}

function isPending(part: ToolPart): boolean {
  const status = part.response?.status;
  return typeof status === "string" && status.includes("pending");
}

function cardSignature(surface: A2UISurface): string {
  return JSON.stringify([
    surface.surfaceId,
    surface.subtitle,
    surface.dataModel,
  ]);
}

export class LatencyTracker {
  private calls: Call[] = [];
  private cards = new Set<string>();

  constructor(
    private readonly log: (line: string) => void = console.info,
    private readonly now: () => number = () => performance.now(),
  ) {}

  call(part: ToolPart): void {
    this.expire();
    this.calls.push({
      key: part.id || part.name,
      name: part.name,
      start: this.now(),
      slow: false,
      kickoffDone: false,
      phases: {},
    });
  }

  response(part: ToolPart): void {
    const call = this.calls.find((c) => c.key === (part.id || part.name));
    if (!call) return;
    if (isPending(part)) call.slow = true;
    else if (call.slow) this.mark(call, "result");
    else this.drop(call); // An ordinary tool: its answer is already in.
  }

  turnComplete(): void {
    for (const call of this.calls) if (call.slow) call.kickoffDone = true;
  }

  /** A slow call started within `CARD_WAIT_MS` is still waiting for its card. */
  awaitingCard(): boolean {
    const now = this.now();
    return this.calls.some(
      (c) =>
        c.slow && c.phases.card === undefined && now - c.start < CARD_WAIT_MS,
    );
  }

  audio(): void {
    for (const call of this.calls) {
      const speakingResult = call.kickoffDone || call.phases.card !== undefined;
      if (call.slow && speakingResult && call.phases.audio === undefined) {
        this.mark(call, "audio");
      }
    }
  }

  /** Every `/state` refresh: a context card not seen before belongs to the oldest open call. */
  surfaces(surfaces: A2UISurface[]): void {
    for (const surface of surfaces) {
      if (surface.kind !== "context_card") continue;
      const signature = cardSignature(surface);
      if (this.cards.has(signature)) continue;
      this.cards.add(signature);
      const call = this.calls.find(
        (c) => c.slow && c.phases.card === undefined,
      );
      if (call) this.mark(call, "card");
    }
    this.expire();
  }

  private mark(call: Call, phase: keyof LatencyPhases): void {
    call.phases[phase] = this.now() - call.start;
    const { card, audio } = call.phases;
    if (card !== undefined && audio !== undefined) this.finish(call);
  }

  private expire(): void {
    const now = this.now();
    for (const call of [...this.calls]) {
      if (now - call.start < LATENCY_TIMEOUT_MS) continue;
      if (call.slow) this.finish(call);
      else this.drop(call);
    }
  }

  private finish(call: Call): void {
    this.log(formatLatency(call.name, call.phases));
    this.drop(call);
  }

  private drop(call: Call): void {
    this.calls = this.calls.filter((c) => c !== call);
  }
}

/** The page's one tracker: the live socket and the `/state` query both feed it. */
export const latency = new LatencyTracker();
