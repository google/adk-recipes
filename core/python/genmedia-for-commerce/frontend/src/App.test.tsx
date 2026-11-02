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

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import App from "./App";
import { resetStudioMock } from "./test/studioMock";
import { setupMockBackend, stubLocalStorage } from "./test/utils";

setupMockBackend();

function renderApp(path: string) {
  window.history.pushState({}, "", path);
  return render(<App />);
}

beforeEach(() => {
  resetStudioMock();
  stubLocalStorage();
});

describe("App", () => {
  it("opens the Studio at /", async () => {
    renderApp("/");

    expect(
      await screen.findByRole("heading", { name: "Studio", level: 1 }),
    ).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Photo Try-On" })).toBeTruthy();
    expect(screen.queryByRole("switch", { name: "Agent mode" })).toBeNull();
  });

  it.each([
    "/studio",
    "/try-on",
    "/video-try-on",
    "/spin",
    "/genmedia-tv",
    "/no-such-page",
  ])("sends %s to the Studio at /", async (path) => {
    renderApp(path);

    expect(
      await screen.findByRole("heading", { name: "Studio", level: 1 }),
    ).toBeTruthy();
    expect(window.location.pathname).toBe("/");
  });
});
