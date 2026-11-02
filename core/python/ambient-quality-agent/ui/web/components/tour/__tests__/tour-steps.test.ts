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

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { TOUR_STEPS, tourSteps } from "../tour-steps";

/** Every component source outside the tour's own folder and the tests. */
function readComponentSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === "__tests__" || entry.name === "tour"
        ? []
        : readComponentSources(full);
    }
    return entry.name.endsWith(".tsx") ? [readFileSync(full, "utf8")] : [];
  });
}

describe("TOUR_STEPS", () => {
  // A step whose element is gone still opens, centered and pointing at nothing,
  // so no other test notices when a refactor drops a `data-tour` wrapper as
  // unused. Reading the sources is what catches it.
  it("points every step at an element the dashboard marks for it", () => {
    const lines = readComponentSources(
      path.resolve(__dirname, "../.."),
    ).flatMap((source) => source.split("\n"));
    const unmarked = TOUR_STEPS.map((step) => step.target).filter(
      (target) =>
        !lines.some(
          (line) => /\btour[=\s]/.test(line) && line.includes(`"${target}"`),
        ),
    );
    expect(unmarked).toEqual([]);
  });
});

// Going right to left, from the insights over to Pipeline, reads backwards;
// but stacked, Home puts the insights on top, and taking Pipeline first would
// climb back up the page.
describe("tourSteps", () => {
  const targets = (sideBySide: boolean) =>
    tourSteps(sideBySide).map((step) => step.target);

  it("takes Pipeline, on the left, before the insights when Home is side by side", () => {
    expect(targets(true).slice(2, 4)).toEqual(["pipeline", "insights-strip"]);
  });

  it("takes the insights, on top, before Pipeline when Home is stacked, and moves nothing else", () => {
    const others = (list: string[]) =>
      list.filter(
        (target) => target !== "pipeline" && target !== "insights-strip",
      );
    expect(targets(false).slice(2, 4)).toEqual(["insights-strip", "pipeline"]);
    expect(others(targets(false))).toEqual(others(targets(true)));
  });
});
