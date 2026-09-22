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
import {
  normalizeWorkspacePath,
  workspacePathFromHref,
  workspacePathFromMention,
} from "../workspace-href";

describe("normalizeWorkspacePath", () => {
  it("canonicalises relative and rooted forms to the same path", () => {
    // Tabs are keyed by path — these must not open two tabs for one file.
    expect(normalizeWorkspacePath("report.md")).toBe("/workspace/report.md");
    expect(normalizeWorkspacePath("./report.md")).toBe("/workspace/report.md");
    expect(normalizeWorkspacePath("/workspace/report.md")).toBe(
      "/workspace/report.md",
    );
  });

  it("keeps nested paths", () => {
    expect(normalizeWorkspacePath("docs/a/b.md")).toBe(
      "/workspace/docs/a/b.md",
    );
  });

  it("rejects absolute host paths", () => {
    // write echoes these back on the local backend; they aren't
    // addressable through the workspace API.
    expect(
      normalizeWorkspacePath("/Users/me/.lha/users/u/report.md"),
    ).toBeNull();
    expect(normalizeWorkspacePath("/etc/passwd")).toBeNull();
  });

  it("rejects traversal and empties", () => {
    expect(normalizeWorkspacePath("../secrets")).toBeNull();
    expect(normalizeWorkspacePath("a/../b")).toBeNull();
    expect(normalizeWorkspacePath("")).toBeNull();
    expect(normalizeWorkspacePath("/workspace")).toBeNull();
  });
});

describe("workspacePathFromHref", () => {
  it("treats a bare relative href as a workspace file", () => {
    expect(workspacePathFromHref("plot.html")).toBe("/workspace/plot.html");
  });

  it("leaves real links alone", () => {
    for (const href of [
      "https://example.com",
      "http://example.com",
      "mailto:a@b.c",
      "tel:123",
      "javascript:alert(1)",
      "data:text/html,x",
      "//evil.com",
      "/lha/sessions",
      "#anchor",
      "?q=1",
    ]) {
      expect(workspacePathFromHref(href)).toBeNull();
    }
  });

  it("returns null for a missing href", () => {
    expect(workspacePathFromHref(undefined)).toBeNull();
  });
});

describe("workspacePathFromMention", () => {
  it("accepts a backticked filename", () => {
    expect(workspacePathFromMention("cats.md")).toBe("/workspace/cats.md");
    expect(workspacePathFromMention("docs/plan.md")).toBe(
      "/workspace/docs/plan.md",
    );
    expect(workspacePathFromMention(" report.md ")).toBe(
      "/workspace/report.md",
    );
  });

  it("ignores prose and shell lines", () => {
    // Models backtick commands far more often than filenames.
    for (const text of [
      "ls -la",
      "npm run build",
      "a sentence.md with spaces",
      "cats",
      "3.14",
      "v1.2",
      "",
    ]) {
      expect(workspacePathFromMention(text)).toBeNull();
    }
  });

  it("ignores urls, schemes and traversal", () => {
    expect(workspacePathFromMention("https://example.com/a.md")).toBeNull();
    expect(workspacePathFromMention("mailto:a@b.co")).toBeNull();
    expect(workspacePathFromMention("../secrets.md")).toBeNull();
  });

  it("ignores an over-long extension (not a filename)", () => {
    expect(workspacePathFromMention("v1.wontbeanextension")).toBeNull();
  });
});
