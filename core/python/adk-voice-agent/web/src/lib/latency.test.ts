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
import { statePollInterval } from "./api";
import {
  CARD_WAIT_MS,
  formatLatency,
  LATENCY_TIMEOUT_MS,
  LatencyTracker,
} from "./latency";
import type { A2UISurface } from "./types";

const PENDING = {
  status: "The function is running asynchronously and the results are pending.",
};

function card(subtitle: string): A2UISurface {
  return {
    surfaceId: "a2ui-ctx-google_search",
    kind: "context_card",
    title: "Search",
    subtitle,
    dataModel: {},
  };
}

function tracker() {
  let now = 0;
  const lines: string[] = [];
  const t = new LatencyTracker(
    (line) => lines.push(line),
    () => now,
  );
  return { t, lines, at: (ms: number) => (now = ms) };
}

describe("formatLatency", () => {
  it("lists the seen phases in seconds since the call", () => {
    expect(
      formatLatency("search_web_grounded", {
        result: 3800,
        card: 3920,
        audio: 5100,
      }),
    ).toBe(
      "[latency] search_web_grounded result +3.8s · card +3.9s · first audio +5.1s",
    );
  });

  it("leaves out phases it never saw", () => {
    expect(formatLatency("search_maps_grounded", { audio: 4200 })).toBe(
      "[latency] search_maps_grounded first audio +4.2s",
    );
    expect(formatLatency("x", {})).toBe("[latency] x nothing seen");
  });
});

describe("LatencyTracker", () => {
  it("logs a slow call once both its card and the spoken result are in", () => {
    const { t, lines, at } = tracker();
    t.surfaces([card("old query")]); // already on screen: not this call's card
    t.call({ id: "c1", name: "search_web_grounded" });
    t.response({ id: "c1", name: "search_web_grounded", response: PENDING });
    at(400);
    t.audio(); // the kickoff sentence, before its turn ends
    t.turnComplete();
    at(3900);
    t.surfaces([card("old query")]);
    t.surfaces([card("weather in austin")]);
    expect(lines).toEqual([]);
    at(5100);
    t.audio();
    t.audio();
    expect(lines).toEqual([
      "[latency] search_web_grounded card +3.9s · first audio +5.1s",
    ]);
  });

  it("records the result when the real response reaches the client", () => {
    const { t, lines, at } = tracker();
    t.call({ id: "c1", name: "search_web_grounded" });
    t.response({ id: "c1", name: "search_web_grounded", response: PENDING });
    at(3800);
    t.response({ id: "c1", name: "search_web_grounded", response: {} });
    at(4000);
    t.surfaces([card("q")]);
    at(4500);
    t.audio();
    expect(lines).toEqual([
      "[latency] search_web_grounded result +3.8s · card +4.0s · first audio +4.5s",
    ]);
  });

  it("ignores tools that answer right away", () => {
    const { t, lines, at } = tracker();
    t.call({ id: "c1", name: "list_threads" });
    t.response({ id: "c1", name: "list_threads", response: { result: [] } });
    t.turnComplete();
    at(1000);
    t.surfaces([card("q")]);
    t.audio();
    at(LATENCY_TIMEOUT_MS + 1000);
    t.surfaces([]);
    expect(lines).toEqual([]);
  });

  it("logs what it saw when a slow call times out without a card", () => {
    const { t, lines, at } = tracker();
    t.call({ name: "start_thread" });
    t.response({ name: "start_thread", response: PENDING });
    t.turnComplete();
    at(2000);
    t.audio();
    at(LATENCY_TIMEOUT_MS);
    t.surfaces([]);
    expect(lines).toEqual(["[latency] start_thread first audio +2.0s"]);
  });
});

describe("awaitingCard", () => {
  it("is true from a slow call's pending response until its card or the wait runs out", () => {
    const { t, at } = tracker();
    t.call({ id: "c1", name: "search_web_grounded" });
    expect(t.awaitingCard()).toBe(false); // not known to be slow yet
    t.response({ id: "c1", name: "search_web_grounded", response: PENDING });
    expect(t.awaitingCard()).toBe(true);
    expect(statePollInterval(t)).toBe(300);
    at(2500);
    t.surfaces([card("q")]);
    expect(t.awaitingCard()).toBe(false);
    expect(statePollInterval(t)).toBe(2000);
  });

  it("stops after CARD_WAIT_MS when no card comes", () => {
    const { t, at } = tracker();
    t.call({ name: "start_thread" });
    t.response({ name: "start_thread", response: PENDING });
    at(CARD_WAIT_MS - 1);
    expect(t.awaitingCard()).toBe(true);
    at(CARD_WAIT_MS);
    expect(t.awaitingCard()).toBe(false);
  });

  it("ignores tools that answer right away", () => {
    const { t } = tracker();
    t.call({ id: "c1", name: "list_threads" });
    t.response({ id: "c1", name: "list_threads", response: { result: [] } });
    expect(t.awaitingCard()).toBe(false);
  });
});
