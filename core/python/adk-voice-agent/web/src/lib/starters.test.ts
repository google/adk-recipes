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
import { appFixture } from "./fixtures";
import {
  appToConnect,
  connectLine,
  GOOGLE_APP,
  starterSuggestions,
} from "./starters";

const apps = (prompts: ReturnType<typeof starterSuggestions>) =>
  prompts.map((s) => s.app ?? "none");

describe("starterSuggestions", () => {
  it("offers only what needs no app when nothing is connected", () => {
    expect(apps(starterSuggestions([]))).toEqual(["none"]);
    expect(
      apps(starterSuggestions([appFixture(GOOGLE_APP), appFixture("github")])),
    ).toEqual(["none"]);
  });

  it("adds each connected app's suggestion", () => {
    const google = appFixture(GOOGLE_APP, { connected: true });
    expect(apps(starterSuggestions([google, appFixture("github")]))).toEqual([
      GOOGLE_APP,
      "none",
    ]);
    const github = appFixture("github", { connected: true });
    expect(apps(starterSuggestions([google, github]))).toEqual([
      "github",
      GOOGLE_APP,
      "none",
    ]);
  });
});

describe("appToConnect", () => {
  it("is Google while it's offered and not connected", () => {
    expect(
      appToConnect([appFixture("github"), appFixture(GOOGLE_APP)])?.name,
    ).toBe(GOOGLE_APP);
    expect(
      appToConnect([appFixture(GOOGLE_APP, { connected: true })]),
    ).toBeUndefined();
    expect(appToConnect([appFixture("github")])).toBeUndefined();
  });
});

describe("connectLine", () => {
  it("says what connecting Google is for, and something plain for any other app", () => {
    expect(connectLine(appFixture(GOOGLE_APP))).toBe(
      "Connect Google so I can check your calendar, email and Drive",
    );
    expect(connectLine(appFixture("github"))).toBe(
      "Connect GitHub so I can use it for you",
    );
  });
});
