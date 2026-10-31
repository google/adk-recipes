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

import { describe, expect, it, vi } from "vitest";
import type { RuntimePreferences } from "../../../lib/api";
import {
  appStatus,
  timezoneChoices,
  timezoneCity,
  voiceModeChoices,
  voiceModeLabel,
} from "./options";
import { VoiceModePicker } from "./PreferencePickers";

describe("timezoneChoices", () => {
  it("puts the browser zone first and drops repeats", () => {
    expect(
      timezoneChoices("America/Chicago", {
        timezone: "Europe/London",
        common_timezones: ["America/Chicago", "Europe/London", "Asia/Tokyo"],
      }),
    ).toEqual(["America/Chicago", "Europe/London", "Asia/Tokyo"]);
  });
});

describe("timezoneCity", () => {
  it.each([
    ["America/Los_Angeles", "Los Angeles"],
    ["America/Argentina/Buenos_Aires", "Buenos Aires"],
    ["UTC", "UTC"],
  ])("%s -> %s", (tz, expected) => {
    expect(timezoneCity(tz)).toBe(expected);
  });
});

describe("appStatus", () => {
  const app = {
    connected: true,
    account_label: "alex@example.com",
    needs_grant: false,
  };

  it("names the account, else says connected", () => {
    expect(appStatus(app)).toBe("alex@example.com");
    expect(appStatus({ ...app, account_label: null })).toBe("Connected");
  });

  it("asks for access the app needs now, and says when it isn't connected", () => {
    expect(appStatus({ ...app, needs_grant: true })).toBe("Needs access");
    expect(appStatus({ ...app, connected: false })).toBe("Not connected");
  });
});

describe("voice mode", () => {
  const prefs = {
    voice_mode: "native",
    available_voice_modes: ["native", "cascaded"],
  } as RuntimePreferences;

  it("offers the server's modes by name, in its order", () => {
    expect(voiceModeChoices(prefs).map((c) => [c.value, c.label])).toEqual([
      ["native", "Native"],
      ["cascaded", "Cascaded"],
    ]);
    expect(voiceModeLabel("cascaded")).toBe("Cascaded");
  });

  it("saves the mode the user picks", () => {
    const onUpdate = vi.fn();
    const picker = VoiceModePicker({ prefs, onUpdate });
    expect(picker.props.value).toBe("native");
    picker.props.onChange("cascaded");
    expect(onUpdate).toHaveBeenCalledWith({ voice_mode: "cascaded" });
  });
});
