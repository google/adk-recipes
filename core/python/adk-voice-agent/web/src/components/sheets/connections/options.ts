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

import type { AppInfo, RuntimePreferences, VoiceMode } from "../../../lib/api";

const VOICE_MODE_TEXT: Record<VoiceMode, { label: string; subtitle: string }> =
  {
    native: {
      label: "Native",
      subtitle: "One speech model hears and speaks. Fastest replies.",
    },
    cascaded: {
      label: "Cascaded",
      subtitle:
        "Transcribes you, thinks in text, then speaks. A little slower.",
    },
  };

export function voiceModeLabel(mode: VoiceMode): string {
  return VOICE_MODE_TEXT[mode]?.label ?? mode;
}

export function voiceModeChoices(
  prefs: Pick<RuntimePreferences, "available_voice_modes">,
): Array<{ value: VoiceMode; label: string; subtitle: string }> {
  return prefs.available_voice_modes.map((mode) => ({
    value: mode,
    label: voiceModeLabel(mode),
    subtitle: VOICE_MODE_TEXT[mode]?.subtitle ?? "",
  }));
}

export function timezoneChoices(
  browserTimezone: string,
  prefs: Pick<RuntimePreferences, "timezone" | "common_timezones">,
): string[] {
  return Array.from(
    new Set([browserTimezone, prefs.timezone, ...prefs.common_timezones]),
  );
}

export function timezoneCity(timezone: string): string {
  const city = timezone.split("/").pop() ?? timezone;
  return city.replace(/_/g, " ");
}

export function appStatus(
  app: Pick<AppInfo, "connected" | "account_label" | "needs_grant">,
): string {
  if (!app.connected) return "Not connected";
  if (app.needs_grant) return "Needs access";
  return app.account_label || "Connected";
}
