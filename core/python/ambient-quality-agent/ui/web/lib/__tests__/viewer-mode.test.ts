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
 * The viewer switch and the storage it isolates.
 *
 * index.html's first script replaces the storage; each test runs it in a
 * window of its own, as a page load would.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { Window } from "happy-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isViewerMode } from "../viewer-mode";

const META = '<meta name="aqua-viewer" content="1">';

const INDEX_HTML = readFileSync(
  path.resolve(__dirname, "../../index.html"),
  "utf8",
);

/** Every `<script>` in index.html, in document order, with its offset. */
const SCRIPTS = [
  ...INDEX_HTML.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g),
].map((match) => ({
  attributes: match[1],
  body: match[2],
  offset: match.index ?? -1,
}));

const STORAGE_SCRIPT = SCRIPTS[0].body;

/** Runs index.html's storage script over `window`, as the browser does
 *  while it parses the shell. */
function runStorageScript(window: unknown, document: unknown): void {
  new Function("window", "document", STORAGE_SCRIPT)(window, document);
}

/** A fresh window whose head holds `head`, after the storage script ran;
 *  `prepare` sees it before. */
function loadShell(head: string, prepare?: (window: Window) => void): Window {
  const window = new Window();
  window.document.head.innerHTML = head;
  prepare?.(window);
  runStorageScript(window, window.document);
  return window;
}

function addViewerMeta(content = "1"): HTMLMetaElement {
  const meta = document.createElement("meta");
  meta.name = "aqua-viewer";
  meta.content = content;
  document.head.prepend(meta);
  return meta;
}

describe("isViewerMode", () => {
  afterEach(() => {
    for (const meta of document.querySelectorAll('meta[name="aqua-viewer"]'))
      meta.remove();
  });

  it("is off without the meta tag", () => {
    expect(isViewerMode()).toBe(false);
  });

  it("is on with the meta tag", () => {
    addViewerMeta();
    expect(isViewerMode()).toBe(true);
  });

  it("is off when the tag says anything but 1", () => {
    addViewerMeta("0");
    expect(isViewerMode()).toBe(false);
  });
});

describe("index.html's storage script", () => {
  it("is a classic, import-free script before the theme and every module", () => {
    // Any script before it, or a module however the build splits the app,
    // could read the real storage first.
    const theme = SCRIPTS.findIndex((s) => s.body.includes("lha.theme"));
    const modules = SCRIPTS.filter((s) => /\btype="module"/.test(s.attributes));

    expect(SCRIPTS[0].attributes).not.toMatch(/\b(type|src)\s*=/);
    expect(STORAGE_SCRIPT).not.toMatch(/\bimport\b/);
    expect(STORAGE_SCRIPT).toContain('meta[name="aqua-viewer"][content="1"]');
    expect(SCRIPTS[0].offset).toBeGreaterThan(INDEX_HTML.indexOf("<head>"));
    expect(theme).toBeGreaterThan(0);
    expect(modules.length).toBeGreaterThan(0);
    for (const module of modules)
      expect(module.offset).toBeGreaterThan(SCRIPTS[0].offset);
  });

  it("leaves the browser's storage alone without the meta tag", () => {
    const real: unknown[] = [];
    const window = loadShell("", (w) => {
      real.push(w.localStorage, w.sessionStorage);
    });

    expect([window.localStorage, window.sessionStorage]).toEqual(real);
    expect(window.localStorage).toBe(real[0]);
  });

  it("keeps everything stored in memory in viewer mode", () => {
    const real: Storage[] = [];
    const window = loadShell(META, (w) => {
      real.push(
        w.localStorage as unknown as Storage,
        w.sessionStorage as unknown as Storage,
      );
    });
    const local = window.localStorage;

    local.setItem("lha.theme", "light");
    window.sessionStorage.setItem("scroll", "120");
    local.setItem("aqua.rail", "[20,80]");
    local.removeItem("aqua.rail");

    expect(local.getItem("lha.theme")).toBe("light");
    expect(window.sessionStorage.getItem("scroll")).toBe("120");
    expect(local).toHaveLength(1);
    expect(local.key(0)).toBe("lha.theme");
    expect(local.key(1)).toBeNull();
    expect(real.map((storage) => storage.length)).toEqual([0, 0]);
    local.clear();
    expect(local).toHaveLength(0);
  });

  it("hides IndexedDB and the Cache API in viewer mode", () => {
    const window = loadShell(META, (w) => {
      // happy-dom has neither, so stand-ins show that the script hides them.
      for (const name of ["indexedDB", "caches"])
        Object.defineProperty(w, name, { value: {}, configurable: true });
    });

    expect(Reflect.get(window, "indexedDB")).toBeUndefined();
    expect(Reflect.get(window, "caches")).toBeUndefined();
  });

  it("stops the page when the storage cannot be replaced", () => {
    const stop = vi.fn();

    expect(() =>
      loadShell(META, (w) => {
        Object.defineProperty(w, "sessionStorage", {
          value: {},
          configurable: false,
        });
        // happy-dom's window has no stop().
        Object.defineProperty(w, "stop", { value: stop });
      }),
    ).toThrow();
    expect(stop).toHaveBeenCalledOnce();
  });
});
