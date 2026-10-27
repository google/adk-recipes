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

import { describe, expect, it } from "vitest";
import { mergeHistoryWithLive } from "@/lib/merge-history-with-live";
import type { ChatMessage } from "@/components/chat/chat-shell";

function msg(
  id: string,
  opts: {
    text?: string;
    role?: ChatMessage["role"];
    createdAt?: number;
    taskId?: string;
    pending?: boolean;
  } = {},
): ChatMessage {
  return {
    id,
    role: opts.role ?? "assistant",
    createdAt: opts.createdAt ?? 0,
    segments: opts.text ? [{ kind: "text", text: opts.text }] : [],
    ...(opts.taskId ? { taskId: opts.taskId } : {}),
    ...(opts.pending ? { pending: true } : {}),
  };
}

// History uses small synthetic timestamps (taskIndex*1000+msgIndex); live uses
// Date.now()-scale values. The merge must sort live after history.
const LIVE = 1_700_000_000_000;

describe("mergeHistoryWithLive", () => {
  it("returns history sorted by createdAt when prev has nothing to preserve", () => {
    const history = [msg("h2", { createdAt: 1 }), msg("h1", { createdAt: 0 })];
    const merged = mergeHistoryWithLive([], history);
    expect(merged.map((m) => m.id)).toEqual(["h1", "h2"]);
  });

  it("returns the prev reference unchanged when the merge is a no-op", () => {
    // Share the exact objects between prev and history: the bailout keys off
    // segment-ref identity (the cache returns stable refs when nothing changed).
    const h1 = msg("h1", { createdAt: 0 });
    const h2 = msg("h2", { createdAt: 1 });
    const prev = [h1, h2];
    const history = [h1, h2];
    expect(mergeHistoryWithLive(prev, history)).toBe(prev);
  });

  it("preserves optimistic user + assistant bubbles not yet in history", () => {
    // The interrupt case: user sent a new turn (t2), but history hasn't caught
    // up — only the prior turn (t1) is in history.
    const histUser = msg("u1", { role: "user", createdAt: 0 });
    const histAsst = msg("a1", { createdAt: 1, taskId: "t1" });
    const optUser = msg("u2", { role: "user", createdAt: LIVE });
    const optAsst = msg("a2", {
      createdAt: LIVE + 1,
      taskId: "t2",
      pending: true,
    });

    const prev = [histUser, histAsst, optUser, optAsst];
    const history = [histUser, histAsst];

    const merged = mergeHistoryWithLive(prev, history);
    expect(merged.map((m) => m.id)).toEqual(["u1", "a1", "u2", "a2"]);
    // The exact optimistic objects survive (so streaming can keep mutating them).
    expect(merged[3]).toBe(optAsst);
  });

  it("dedups the user bubble by id — history content wins", () => {
    const optUser = msg("u1", {
      role: "user",
      text: "optimistic",
      createdAt: LIVE,
    });
    const histUser = msg("u1", {
      role: "user",
      text: "canonical",
      createdAt: 0,
    });
    const merged = mergeHistoryWithLive([optUser], [histUser]);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toBe(histUser);
  });

  it("drops an optimistic assistant once its task lands in history", () => {
    const optAsst = msg("a-opt", {
      createdAt: LIVE,
      taskId: "t2",
      text: "stop",
    });
    const histAsst = msg("a-srv", {
      createdAt: 5,
      taskId: "t2",
      text: "full reply",
    });
    const merged = mergeHistoryWithLive([optAsst], [histAsst]);
    // No duplicate assistant bubble for task t2; the server copy is canonical.
    expect(merged.map((m) => m.id)).toEqual(["a-srv"]);
  });

  it("keeps a resubscribe live bubble until its task is in history", () => {
    const live = msg("live:t3", {
      createdAt: LIVE,
      taskId: "t3",
      pending: true,
    });
    const histA = msg("a1", { createdAt: 0, taskId: "t1" });

    // t3 not yet in history → keep the live bubble.
    let merged = mergeHistoryWithLive([histA, live], [histA]);
    expect(merged.map((m) => m.id)).toEqual(["a1", "live:t3"]);
    expect(merged[1]).toBe(live);

    // t3 now in history → drop the live bubble (no duplicate).
    const histT3 = msg("a3", { createdAt: 10, taskId: "t3" });
    merged = mergeHistoryWithLive([histA, live], [histA, histT3]);
    expect(merged.map((m) => m.id)).toEqual(["a1", "a3"]);
  });

  it("sorts by createdAt so a task-order race can't scramble the screen", () => {
    // History arrives with messages out of array order; the synthetic createdAt
    // is the source of truth.
    const a = msg("a", { createdAt: 2 });
    const b = msg("b", { createdAt: 0 });
    const c = msg("c", { createdAt: 1 });
    const live = msg("live", { createdAt: LIVE, taskId: "tX", pending: true });
    const merged = mergeHistoryWithLive([live], [a, b, c]);
    expect(merged.map((m) => m.id)).toEqual(["b", "c", "a", "live"]);
  });

  it("is a stable sort: equal createdAt keeps history-then-prev order", () => {
    const h1 = msg("h1", { createdAt: 5 });
    const h2 = msg("h2", { createdAt: 5 });
    const opt = msg("opt", { createdAt: 5, role: "system" });
    const merged = mergeHistoryWithLive([opt], [h1, h2]);
    expect(merged.map((m) => m.id)).toEqual(["h1", "h2", "opt"]);
  });
});
