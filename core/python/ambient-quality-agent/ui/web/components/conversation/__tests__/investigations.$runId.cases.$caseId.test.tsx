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
import { archiveNote } from "@/src/routes/investigations.$runId.cases.$caseId";
import type { CaseConversation } from "@/lib/aqua-api";

describe("archiveNote", () => {
  it("describes an unarchived trajectory and its investigation", () => {
    const note = archiveNote({
      status: "not_archived",
      turn_count: 0,
      turns_returned: 0,
      turns: [],
    } as unknown as CaseConversation);
    expect(note).toContain("This trajectory was never archived.");
    expect(note).toContain("or its investigation could not assemble it.");
  });

  it("describes an expired trajectory archive", () => {
    const note = archiveNote({
      status: "ok",
      turn_count: 3,
      turns_returned: 0,
      turns: [],
    } as unknown as CaseConversation);
    expect(note).toContain(
      "The trajectory is gone; this page is what is left of it.",
    );
  });
});
