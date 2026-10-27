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
import { qk } from "../query-keys";

describe("query-keys factory", () => {
  it("produces deterministic, structurally comparable keys", () => {
    expect(qk.state("ctx")).toEqual(["lha", "state", "ctx"]);
    expect(qk.state()).toEqual(["lha", "state", null]);
    expect(qk.sessions("user", "q")).toEqual(["lha", "sessions", "user", "q"]);
    expect(qk.tasks("c1")).toEqual(["lha", "contexts", "c1", "tasks"]);
  });

  it("handles empty query strings", () => {
    const a = qk.sessions("user", "");
    const b = qk.sessions("scheduler", "x");
    expect(a.slice(0, 2)).toEqual(["lha", "sessions"]);
    expect(b.slice(0, 2)).toEqual(["lha", "sessions"]);
  });
});
