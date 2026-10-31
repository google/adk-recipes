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
import { threadFixture } from "./fixtures";
import {
  appConnectedTurn,
  focusPayload,
  HOME,
  isNavigationTool,
  NAVIGATE_TOOL,
  navigate,
  navigateApp,
  navigateView,
  type View,
} from "./navigation";

const portland: View = { view: "project", project: "projects/portland-trip" };
const thread7: View = {
  view: "thread",
  threadId: "thread-7",
  project: "projects/portland-trip",
};

describe("navigate", () => {
  it("moves between home, projects, and threads", () => {
    expect(navigate(HOME, portland)).toEqual(portland);
    expect(navigate(portland, thread7)).toEqual(thread7);
    expect(navigate(thread7, HOME)).toBe(HOME);
  });

  it("returns the same view when nothing changes, so React skips the render", () => {
    expect(navigate(HOME, { view: "home" })).toBe(HOME);
    expect(navigate(portland, { ...portland })).toBe(portland);
    expect(navigate(thread7, { ...thread7 })).toBe(thread7);
    expect(navigate(thread7, { ...thread7, title: "Add greet" })).toEqual({
      ...thread7,
      title: "Add greet",
    });
  });
});

describe("focusPayload", () => {
  it("formats home, project, and thread views for POST /api/v1/focus", () => {
    expect(focusPayload(HOME)).toEqual({ view: "home" });
    expect(focusPayload(portland)).toEqual({
      view: "project",
      project: "projects/portland-trip",
    });
    expect(focusPayload(thread7)).toEqual({
      view: "thread",
      thread_id: "thread-7",
      project: "projects/portland-trip",
    });
  });
});

describe("navigateView", () => {
  const threads = [
    threadFixture("executing", {
      thread_id: "thread-7",
      project: "projects/portland-trip",
    }),
  ];

  it("follows home and project targets", () => {
    expect(navigateView({ target: "home" }, threads)).toBe(HOME);
    expect(
      navigateView({ target: "project", project: "code/payments" }, threads),
    ).toEqual({ view: "project", project: "code/payments" });
  });

  it("opens a thread by thread_id, finding its project and title in /state", () => {
    expect(
      navigateView({ target: "thread", thread_id: "thread-7" }, threads),
    ).toEqual({
      view: "thread",
      threadId: "thread-7",
      project: "projects/portland-trip",
      title: "Add greet",
    });
    expect(
      navigateView(
        {
          target: "thread",
          thread_id: "thread-9",
          project: "scratch/2026-10-25-note",
          title: "Quick note",
        },
        threads,
      ),
    ).toEqual({
      view: "thread",
      threadId: "thread-9",
      project: "scratch/2026-10-25-note",
      title: "Quick note",
    });
  });

  it("ignores what it can't place", () => {
    expect(navigateView({ target: "project" }, threads)).toBeNull();
    expect(
      navigateView({ target: "thread", thread_id: "thread-unknown" }, threads),
    ).toBeNull();
    expect(navigateView({ target: "settings" }, threads)).toBeNull();
    expect(navigateView({}, threads)).toBeNull();
  });

  it("recognizes only the navigate tool name", () => {
    expect(isNavigationTool(NAVIGATE_TOOL)).toBe(true);
    expect(isNavigationTool("open_thread")).toBe(false);
    expect(isNavigationTool("start_thread")).toBe(false);
  });
});

describe("navigateApp", () => {
  it("opens an app's Settings page via app, and leaves screens to navigateView", () => {
    expect(navigateApp({ target: "app", app: "github" })).toBe("github");
    expect(navigateApp({ target: "app", app: "google_workspace" })).toBe(
      "google_workspace",
    );
    expect(navigateView({ target: "app", app: "github" }, [])).toBeNull();
    expect(navigateApp({ target: "app" })).toBeNull();
    expect(navigateApp({ target: "home" })).toBeNull();
  });

  it("tells the voice when an app finished connecting", () => {
    expect(appConnectedTurn("Google Workspace")).toBe(
      "[APP CONNECTED] Google Workspace",
    );
  });
});
