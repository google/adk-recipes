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
  initialViewerState,
  tabId,
  viewerReducer,
  type ViewerState,
} from "../viewer-store";

const md = { name: "spec.md", mimeType: "text/markdown", bytes: "aGk=" };
const png = { name: "plot.png", mimeType: "image/png", bytes: "aW1n" };

describe("viewerReducer", () => {
  it("open adds a tab and makes it active", () => {
    const s = viewerReducer(initialViewerState, { type: "open", artifact: md });
    expect(s.tabs).toHaveLength(1);
    expect(s.tabs[0].id).toBe(tabId(md));
    expect(s.activeId).toBe(tabId(md));
  });

  it("re-opening the same artifact dedupes and activates the existing tab", () => {
    let s = viewerReducer(initialViewerState, { type: "open", artifact: md });
    s = viewerReducer(s, { type: "open", artifact: png });
    s = viewerReducer(s, { type: "open", artifact: md });
    expect(s.tabs).toHaveLength(2);
    expect(s.activeId).toBe(tabId(md));
  });

  it("activate switches the active tab only when it exists", () => {
    let s = viewerReducer(initialViewerState, { type: "open", artifact: md });
    s = viewerReducer(s, { type: "open", artifact: png });
    s = viewerReducer(s, { type: "activate", id: tabId(md) });
    expect(s.activeId).toBe(tabId(md));
    const noop = viewerReducer(s, { type: "activate", id: "missing" });
    expect(noop).toBe(s);
  });

  it("close removes the tab and reselects a neighbor", () => {
    let s: ViewerState = initialViewerState;
    s = viewerReducer(s, { type: "open", artifact: md });
    s = viewerReducer(s, { type: "open", artifact: png });
    // active is png (last opened); closing it falls back to the previous tab.
    s = viewerReducer(s, { type: "close", id: tabId(png) });
    expect(s.tabs).toHaveLength(1);
    expect(s.activeId).toBe(tabId(md));
  });

  it("closing the last tab nulls the active id", () => {
    let s = viewerReducer(initialViewerState, { type: "open", artifact: md });
    s = viewerReducer(s, { type: "close", id: tabId(md) });
    expect(s.tabs).toHaveLength(0);
    expect(s.activeId).toBeNull();
  });

  it("closing a non-active tab keeps the active id", () => {
    let s = viewerReducer(initialViewerState, { type: "open", artifact: md });
    s = viewerReducer(s, { type: "open", artifact: png });
    // active is png; close the inactive md tab.
    s = viewerReducer(s, { type: "close", id: tabId(md) });
    expect(s.activeId).toBe(tabId(png));
  });

  it("clear empties tabs and active id", () => {
    let s = viewerReducer(initialViewerState, { type: "open", artifact: md });
    s = viewerReducer(s, { type: "clear" });
    expect(s).toEqual(initialViewerState);
  });
});
