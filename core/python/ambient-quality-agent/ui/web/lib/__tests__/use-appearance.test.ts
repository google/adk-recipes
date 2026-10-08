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

import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useAppearance, useResolvedTheme } from "@/lib/use-appearance";

// happy-dom answers every media query `false`, which would silently pin the
// system preference to light and make "follows the OS" untestable in the one
// direction that matters. This is a controllable stand-in: `setSystemDark`
// flips the answer and fires `change`, which is what an OS switching at
// sunset does to a page that is already open.
let systemDark = false;
const mediaListeners = new Set<() => void>();

function setSystemDark(next: boolean) {
  systemDark = next;
  for (const l of mediaListeners) l();
}

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      media: query,
      get matches() {
        return query.includes("dark") && systemDark;
      },
      addEventListener: (_: string, l: () => void) =>
        void mediaListeners.add(l),
      removeEventListener: (_: string, l: () => void) =>
        void mediaListeners.delete(l),
    }),
  });
});

// happy-dom doesn't ship a localStorage; install a minimal in-memory one.
beforeAll(() => {
  if (typeof window.localStorage === "undefined") {
    const store = new Map<string, string>();
    const mock: Storage = {
      get length() {
        return store.size;
      },
      clear: () => store.clear(),
      getItem: (k) => (store.has(k) ? (store.get(k) as string) : null),
      key: (i) => Array.from(store.keys())[i] ?? null,
      removeItem: (k) => void store.delete(k),
      setItem: (k, v) => void store.set(k, String(v)),
    };
    Object.defineProperty(window, "localStorage", {
      value: mock,
      configurable: true,
    });
  }
});

function resetDom() {
  window.localStorage.clear();
  systemDark = false;
  mediaListeners.clear();
  const root = document.documentElement;
  root.classList.remove("dark");
  delete root.dataset.skin;
  for (const n of document.head.querySelectorAll("#lha-theme-color")) {
    n.remove();
  }
}

beforeEach(resetDom);
afterEach(resetDom);

function themeColorMeta(): HTMLMetaElement {
  const meta = document.createElement("meta");
  meta.id = "lha-theme-color";
  meta.setAttribute("name", "theme-color");
  document.head.appendChild(meta);
  return meta;
}

describe("useAppearance", () => {
  it("setSkin applies the data-skin attribute and persists it", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setSkin("ocean"));
    expect(result.current.skin).toBe("ocean");
    expect(document.documentElement.dataset.skin).toBe("ocean");
    expect(window.localStorage.getItem("lha.skin")).toBe("ocean");
  });

  it("setSkin('default') removes the attribute (no skin styling)", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setSkin("ember"));
    act(() => result.current.setSkin("default"));
    expect(result.current.skin).toBe("default");
    expect(document.documentElement.dataset.skin).toBeUndefined();
    expect(window.localStorage.getItem("lha.skin")).toBe("default");
  });

  it("setTheme applies the dark class, persists, and updates the theme-color meta", () => {
    const meta = themeColorMeta();
    const { result } = renderHook(() => useAppearance());

    act(() => result.current.setTheme("dark"));
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("lha.theme")).toBe("dark");
    // meta tracks the resolved theme with the exact --background hex.
    expect(meta.getAttribute("content")).toBe("#101010");

    act(() => result.current.setTheme("light"));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(meta.getAttribute("content")).toBe("#fefdfb");
  });

  it("defaults to following the system", () => {
    setSystemDark(true);
    const { result } = renderHook(() => useAppearance());
    expect(result.current.themeChoice).toBe("system");
    expect(result.current.theme).toBe("dark");
  });

  it("resolves 'system' against the OS in both directions", () => {
    const meta = themeColorMeta();
    const { result } = renderHook(() => useAppearance());

    setSystemDark(true);
    act(() => result.current.setTheme("system"));
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    setSystemDark(false);
    act(() => result.current.setTheme("system"));
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(meta.getAttribute("content")).toBe("#fefdfb");
    // The choice persists, not the theme it happened to resolve to -- storing
    // "light" here would freeze the page at whatever the OS said once.
    expect(window.localStorage.getItem("lha.theme")).toBe("system");
  });

  // The whole point of "system" over a toggle: an OS that switches at sunset
  // has to reach a tab that is already open.
  it("follows the OS changing while the page is open", async () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setTheme("system"));
    expect(result.current.theme).toBe("light");

    act(() => setSystemDark(true));
    await waitFor(() => expect(result.current.theme).toBe("dark"));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("ignores the OS once a theme is chosen explicitly", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => result.current.setTheme("light"));

    act(() => setSystemDark(true));
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("keeps an explicit choice saved by an earlier version", () => {
    // The stored vocabulary gained "system"; it did not lose "light"/"dark",
    // so nobody who had picked one gets moved off it by this change.
    window.localStorage.setItem("lha.theme", "light");
    setSystemDark(true);
    const { result } = renderHook(() => useAppearance());
    expect(result.current.themeChoice).toBe("light");
    expect(result.current.theme).toBe("light");
  });

  it("syncs skin from a cross-tab storage event", () => {
    const { result } = renderHook(() => useAppearance());
    act(() => {
      window.localStorage.setItem("lha.skin", "moss");
      window.dispatchEvent(
        new StorageEvent("storage", { key: "lha.skin", newValue: "moss" }),
      );
    });
    expect(result.current.skin).toBe("moss");
    expect(document.documentElement.dataset.skin).toBe("moss");
  });

  it("syncs theme from a cross-tab storage event and treats removal as system", () => {
    setSystemDark(true);
    const { result } = renderHook(() => useAppearance());
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "lha.theme", newValue: "light" }),
      );
    });
    expect(result.current.themeChoice).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    // A null newValue is the key cleared in another tab, which is the state of
    // never having chosen -- so back to the system, which here is dark.
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "lha.theme", newValue: null }),
      );
    });
    expect(result.current.themeChoice).toBe("system");
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});

describe("useResolvedTheme", () => {
  it("reflects the initial dark class on <html>", () => {
    document.documentElement.classList.add("dark");
    const { result } = renderHook(() => useResolvedTheme());
    expect(result.current).toBe("dark");
  });

  // Regression: a separate component toggling the theme in the same tab does
  // not fire a `storage` event, so consumers must track the `dark` class itself
  // rather than their own useState — otherwise code blocks keep the stale theme.
  // Each direction renders fresh (one MutationObserver, one transition) to keep
  // the observer's async delivery deterministic under happy-dom.
  it("reacts to the dark class being added in the same tab", async () => {
    const { result } = renderHook(() => useResolvedTheme());
    expect(result.current).toBe("light");
    act(() => {
      document.documentElement.classList.add("dark");
    });
    await waitFor(() => expect(result.current).toBe("dark"));
  });

  it("reacts to the dark class being removed in the same tab", async () => {
    document.documentElement.classList.add("dark");
    const { result } = renderHook(() => useResolvedTheme());
    expect(result.current).toBe("dark");
    act(() => {
      document.documentElement.classList.remove("dark");
    });
    await waitFor(() => expect(result.current).toBe("light"));
  });
});
