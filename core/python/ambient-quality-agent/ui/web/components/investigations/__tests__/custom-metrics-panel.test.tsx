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
import { render, screen } from "@testing-library/react";
import type { MetricDetail } from "@/lib/aqua-api";
import { CustomMetricsPanel } from "../custom-metrics-panel";

const RAN: MetricDetail = {
  kind: "local",
  expected: "It answers.",
  threshold: 1,
};
const PREDEFINED: MetricDetail = {
  kind: "predefined",
  not_run: "it only parameterizes a metric computed elsewhere",
};

describe("CustomMetricsPanel", () => {
  it("labels every kind the loader can emit", () => {
    render(
      <CustomMetricsPanel
        detail={{
          ran: RAN,
          remote: { kind: "remote" },
          judged: { kind: "judged", expected: "It is polite.", threshold: 0.7 },
          named: PREDEFINED,
          bad: { kind: "refused" },
        }}
      />,
    );

    expect(screen.getByText("python · local")).toBeTruthy();
    expect(screen.getByText("python · eval service")).toBeTruthy();
    expect(screen.getByText("llm as judge · eval service")).toBeTruthy();
    expect(screen.getByText("predefined · not run")).toBeTruthy();
    expect(screen.getByText("refused")).toBeTruthy();
    expect(screen.getByText("Eval service errors")).toBeTruthy();
  });

  it("shows a judge with no threshold as scores only, with its mean", () => {
    render(
      <CustomMetricsPanel
        detail={{
          rated: {
            kind: "judged",
            threshold: null,
            scores: { count: 3, mean: 3, min: 1, max: 5 },
          },
        }}
      />,
    );

    expect(screen.getByText("scores only · mean 3.00")).toBeTruthy();
  });

  it("names the deployment's own metric list beside a predefined row", () => {
    render(<CustomMetricsPanel detail={{ named: PREDEFINED }} />);

    expect(screen.getByText(/MULTI_TURN_METRICS/)).toBeTruthy();
    expect(screen.getByText(/SINGLE_TURN_METRICS/)).toBeTruthy();
  });

  it("omits that note when nothing on the table is predefined", () => {
    render(
      <CustomMetricsPanel detail={{ ran: RAN, bad: { kind: "refused" } }} />,
    );

    expect(screen.queryByText(/MULTI_TURN_METRICS/)).toBeNull();
  });
});
