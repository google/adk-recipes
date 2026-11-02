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

import { render } from "@testing-library/react";
import { setupServer } from "msw/node";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { handlers } from "./mockServer";

export const server = setupServer(...handlers);

/** Starts the msw backend for the test file. */
export function setupMockBackend(): void {
  beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
  afterEach(() => server.resetHandlers());
  afterAll(() => server.close());
}

/** Renders `routes` inside a MemoryRouter positioned at `path`. */
export function renderAt(path: string, routes: Record<string, ReactElement>) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {Object.entries(routes).map(([route, element]) => (
          <Route key={route} path={route} element={element} />
        ))}
      </Routes>
    </MemoryRouter>,
  );
}

export const imageFile = (name = "image.png") =>
  new File(["x"], name, { type: "image/png" });

/** An empty in-memory localStorage for the test (Node 25's own, without a file, shadows jsdom's). */
export function stubLocalStorage(): void {
  const items = new Map<string, string>();
  const storage: Pick<Storage, "getItem" | "setItem" | "removeItem" | "clear"> =
    {
      getItem: (key) => items.get(key) ?? null,
      setItem: (key, value) => {
        items.set(key, String(value));
      },
      removeItem: (key) => {
        items.delete(key);
      },
      clear: () => items.clear(),
    };
  vi.stubGlobal("localStorage", storage);
}
