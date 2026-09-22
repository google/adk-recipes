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
import { isAgentWorkingFile, workspacePathForName } from "../workspace-files";

const files = new Map([
  ["/workspace/report.md", 1],
  ["/workspace/docs/plan.md", 2],
  ["/workspace/notes/plan.md", 3],
]);

describe("workspacePathForName", () => {
  it("resolves a unique basename to its path", () => {
    expect(workspacePathForName(files, "report.md")).toBe(
      "/workspace/report.md",
    );
  });

  it("refuses an ambiguous basename", () => {
    // Opening the wrong plan.md and letting it be edited is worse than
    // leaving the attachment read-only.
    expect(workspacePathForName(files, "plan.md")).toBeNull();
  });

  it("returns null for a file that isn't in the workspace", () => {
    expect(workspacePathForName(files, "elsewhere.md")).toBeNull();
  });

  it("returns null before the listing has loaded", () => {
    expect(workspacePathForName(undefined, "report.md")).toBeNull();
  });

  it("does not match on a path suffix", () => {
    expect(workspacePathForName(files, "md")).toBeNull();
    expect(workspacePathForName(files, "docs/plan.md")).toBeNull();
  });
});
describe("isAgentWorkingFile", () => {
  it("treats the agent's own bookkeeping as working material", () => {
    expect(isAgentWorkingFile("/workspace/plan.md")).toBe(true);
    expect(isAgentWorkingFile("/workspace/TODO.md")).toBe(true);
    expect(isAgentWorkingFile("/workspace/scripts/md_to_pdf.py")).toBe(true);
    expect(isAgentWorkingFile("/workspace/.agents/skills/x/SKILL.md")).toBe(
      true,
    );
    expect(isAgentWorkingFile("/workspace/.ruff_cache/x")).toBe(true);
  });

  it("treats everything else as the user's", () => {
    expect(isAgentWorkingFile("/workspace/report.md")).toBe(false);
    expect(isAgentWorkingFile("/workspace/cats.pdf")).toBe(false);
    // A plan inside a project folder is that project's document.
    expect(isAgentWorkingFile("/workspace/acme/plan.md")).toBe(false);
    // Not fooled by a name that merely contains one.
    expect(isAgentWorkingFile("/workspace/my-plan.md")).toBe(false);
    expect(isAgentWorkingFile("/workspace/scripts-notes.md")).toBe(false);
  });
});
