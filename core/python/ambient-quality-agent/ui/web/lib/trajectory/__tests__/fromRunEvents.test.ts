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
import type { InvestigationEvent } from "@/lib/aqua-api";
import { fromRunEvents } from "../fromRunEvents";

const EVENTS: InvestigationEvent[] = [
  {
    run_id: "r1",
    created_at: "2026-08-28T18:35:00Z",
    source: "investigate",
    text: "**Investigate cluster 1**\nLooking into create_ticket.",
  },
  {
    run_id: "r1",
    created_at: "2026-08-28T18:35:10Z",
    source: "investigate",
    text: "- **Running code:**\n```python\nimport pandas as pd\n```",
  },
];

describe("fromRunEvents", () => {
  it("gives each node one top-level record, in order", () => {
    const doc = fromRunEvents(EVENTS);
    expect(doc.records.length).toBeGreaterThanOrEqual(2);
  });

  it("gives a Running-code bullet its own child tool record", () => {
    const doc = fromRunEvents(EVENTS);
    const codeStep = doc.records.find((r) => r.kind === "tool");
    expect(codeStep).toBeDefined();
    expect(codeStep?.toolName).toBe("execute_code");
  });

  it("keeps the full event markdown on the top-level record", () => {
    const doc = fromRunEvents([
      ...EVENTS,
      {
        run_id: "r1",
        created_at: "2026-08-28T18:35:20Z",
        source: "investigate",
        text: "   ",
      },
      {
        run_id: "r1",
        created_at: "2026-08-28T18:35:30Z",
        source: null,
        text: "",
      },
    ]);
    expect(doc.records[0].text).toBe(EVENTS[0].text);
    const systemRecords = doc.records.filter((r) => r.kind === "system");
    expect(systemRecords[2].text).toBe("investigate");
    expect(systemRecords[3].text).toBe("step 4");
  });
});
