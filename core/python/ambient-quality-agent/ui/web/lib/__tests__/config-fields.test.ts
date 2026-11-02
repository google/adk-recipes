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
 * Grouping the config must never lose a field.
 *
 * That is the whole reason the "Other" bucket exists: the curated groups are a
 * hand-written list, a new config key will not be on it, and a reader looking
 * for a setting that the agent reports but the dashboard silently drops has no
 * way to tell that from the setting not existing.
 */
import { describe, expect, it } from "vitest";

import { configLabel, configValue, groupConfig } from "../config-fields";

describe("groupConfig", () => {
  it("orders the groups as curated, not as the payload arrives", () => {
    const groups = groupConfig({
      base_model: "gemini",
      observed_agent_name: "travel_desk_agent",
    });
    expect(groups.map((g) => g.title)).toEqual(["Observed agent", "Models"]);
  });

  it("emits no heading for a group with nothing in it", () => {
    const groups = groupConfig({ observed_agent_name: "a" });
    expect(groups).toHaveLength(1);
  });

  it("puts an unknown key in Other rather than dropping it", () => {
    const groups = groupConfig({ a_brand_new_knob: 1 });
    expect(groups).toEqual([
      { title: "Other", entries: [["a_brand_new_knob", 1]] },
    ]);
  });

  it("keeps an explicitly null value as a row", () => {
    // Presence, not truthiness: "set to nothing" and "not a field" are
    // different answers and a reader needs to tell them apart.
    const groups = groupConfig({ aqa_engine_id: null });
    expect(groups[0].entries).toEqual([["aqa_engine_id", null]]);
  });

  it("does not let a curated key missing from the payload suppress anything", () => {
    const groups = groupConfig({ project_id: "p" });
    expect(groups[0].entries).toEqual([["project_id", "p"]]);
  });
});

describe("configLabel", () => {
  it("uses the curated label where there is one", () => {
    expect(configLabel("aqa_engine_id")).toBe("AQuA's own engine id");
  });

  it("opens out the underscores otherwise", () => {
    expect(configLabel("telemetry_dataset")).toBe("telemetry dataset");
  });
});

describe("configValue", () => {
  it("renders absent values as a dash", () => {
    expect(configValue(null)).toEqual({ kind: "text", text: "—" });
    expect(configValue("")).toEqual({ kind: "text", text: "—" });
  });

  it("does not treat 0 or false as absent", () => {
    // A cadence of 0 and an unset cadence are different deployments.
    expect(configValue(0)).toEqual({ kind: "text", text: "0" });
    expect(configValue(false)).toEqual({ kind: "text", text: "no" });
  });

  it("renders booleans as yes/no", () => {
    expect(configValue(true)).toEqual({ kind: "text", text: "yes" });
  });

  it("renders a list as pills and an empty list as none", () => {
    expect(configValue(["a", "b"])).toEqual({
      kind: "pills",
      items: ["a", "b"],
    });
    expect(configValue([])).toEqual({ kind: "text", text: "none" });
  });

  it("flattens a model record", () => {
    expect(
      configValue({ model: "gemini-3.5-flash", location: "global" }),
    ).toEqual({
      kind: "text",
      text: "gemini-3.5-flash (global)",
    });
    expect(configValue({ model: "gemini-3.5-flash" })).toEqual({
      kind: "text",
      text: "gemini-3.5-flash",
    });
  });

  it("falls back to JSON for an object it does not recognise", () => {
    expect(configValue({ a: 1 })).toEqual({ kind: "text", text: '{"a":1}' });
  });
});
