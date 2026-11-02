/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { beforeEach, describe, expect, it } from "vitest";
import { stubLocalStorage } from "../../test/utils";
import { loadStudioId } from "./studioId";

const KEY = "genmedia.studioId";

beforeEach(() => stubLocalStorage());

describe("loadStudioId", () => {
  it("keeps a saved id the server accepts, up to 57 characters", () => {
    for (const id of ["a", "ab_cd-12", "x".repeat(57)]) {
      localStorage.setItem(KEY, id);
      expect(loadStudioId()).toBe(id);
    }
  });

  it("replaces a saved id the server would refuse", () => {
    for (const id of ["x".repeat(58), "a.b", "a b", "jobs", ""]) {
      localStorage.setItem(KEY, id);
      const fresh = loadStudioId();
      expect(fresh).toMatch(/^[0-9a-f]{12}$/);
      expect(localStorage.getItem(KEY)).toBe(fresh);
    }
  });

  it("creates one on the first visit", () => {
    expect(loadStudioId()).toMatch(/^[0-9a-f]{12}$/);
  });
});
