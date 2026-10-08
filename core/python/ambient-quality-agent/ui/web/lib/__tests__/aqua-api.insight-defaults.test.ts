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

// The detail pane against an engine with no dig phase. Every field it reads
// unguarded has to survive being absent, because absent is what our engine
// sends and the pane is the only view that crashed rather than rendering
// blanks.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  insightQuery,
  isDiagnosed,
  noneDiagnosed,
  isOccurrenceDiagnosed,
} from "@/lib/aqua-api";

function respondWith(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("insightQuery", () => {
  it("fills the occurrence fields the pane reads unguarded", async () => {
    // Exactly what our engine sends: an occurrence with none of the dig
    // phase's fields on it.
    respondWith({
      insight: { insight_id: "ins-1", label: "calls the wrong tool" },
      occurrences: [
        { occurrence_id: "o-1", created_at: "2026-09-01T00:00:00Z" },
      ],
    });

    const detail = await insightQuery("ins-1").queryFn();
    const [occurrence] = detail.occurrences;

    // `occurrence.evidence_case_ids.length` is read with no guard; undefined
    // here is the TypeError that blanked the pane.
    expect(occurrence.evidence_case_ids).toEqual([]);
    expect(occurrence.cases_checked).toBe(0);
    expect(occurrence.diagnosis).toBe("");
    expect(occurrence.confidence).toBe(0);
  });

  it("fills the insight fields too, which this query used to skip", async () => {
    respondWith({ insight: { insight_id: "ins-1" }, occurrences: [] });

    const { insight } = await insightQuery("ins-1").queryFn();

    expect(insight.diagnosis).toBe("");
    expect(insight.impact).toBe(0);
  });

  it("does not overwrite what the engine did send", async () => {
    respondWith({
      insight: { insight_id: "ins-1", diagnosis: "drops the tenant id" },
      occurrences: [
        { occurrence_id: "o-1", evidence_case_ids: ["t-1"], cases_checked: 3 },
      ],
    });

    const detail = await insightQuery("ins-1").queryFn();

    expect(detail.insight.diagnosis).toBe("drops the tenant id");
    expect(detail.occurrences[0].evidence_case_ids).toEqual(["t-1"]);
    expect(detail.occurrences[0].cases_checked).toBe(3);
  });

  it("survives a payload with neither half", async () => {
    respondWith({});
    const detail = await insightQuery("ins-1").queryFn();
    expect(detail.occurrences).toEqual([]);
  });
});

describe("isDiagnosed", () => {
  it("counts an autorater diagnosis", () => {
    expect(isDiagnosed({ diagnosis: "drops the tenant id" })).toBe(true);
  });

  it("counts a recorded root cause with no autorater diagnosis", () => {
    expect(isDiagnosed({ diagnosis: "", has_root_cause: true })).toBe(true);
  });

  it("holds an insight with neither undiagnosed", () => {
    expect(isDiagnosed({ diagnosis: "", has_root_cause: false })).toBe(false);
    expect(isDiagnosed({})).toBe(false);
  });
});

describe("isOccurrenceDiagnosed", () => {
  // A sighting carries its records inline, so it has no `has_root_cause`.
  it("counts records attached to the sighting", () => {
    const record = { root_cause_id: "rc-1" } as never;
    expect(
      isOccurrenceDiagnosed({ diagnosis: "", root_causes: [record] }),
    ).toBe(true);
  });

  it("holds a sighting with no diagnosis and no records undiagnosed", () => {
    expect(isOccurrenceDiagnosed({ diagnosis: "", root_causes: [] })).toBe(
      false,
    );
  });
});

describe("noneDiagnosed", () => {
  // Regression: this compared against `undefined`, but every caller reads a
  // page that has been through `withDefaults`, where a missing diagnosis is
  // "". So the banner could never fire, and an all-blank list looked like a
  // pipeline that had found nothing rather than an engine without the phase.
  it("fires on a page whose diagnoses have already been defaulted", () => {
    expect(noneDiagnosed([{ diagnosis: "" }, { diagnosis: "" }])).toBe(true);
  });

  it("still fires when the fields are absent entirely", () => {
    expect(noneDiagnosed([{}, {}])).toBe(true);
  });

  it("does not fire when any insight carries a diagnosis", () => {
    expect(noneDiagnosed([{ diagnosis: "" }, { diagnosis: "a cause" }])).toBe(
      false,
    );
  });

  it("does not fire on an empty page, which says nothing about the engine", () => {
    expect(noneDiagnosed([])).toBe(false);
  });

  // A recorded root cause confirms root-cause capability even without an autorater diagnosis.
  it("does not fire when any insight carries a recorded root cause", () => {
    expect(
      noneDiagnosed([
        { diagnosis: "" },
        { diagnosis: "", has_root_cause: true },
      ]),
    ).toBe(false);
  });
});
