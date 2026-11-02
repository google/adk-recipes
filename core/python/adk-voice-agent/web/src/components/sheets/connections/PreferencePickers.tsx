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

import { type ReactNode, useState } from "react";
import type {
  HarnessInfo,
  PreferencesPatch,
  RuntimePreferences,
} from "../../../lib/api";
import { HarnessBrandIcon } from "../../BrandIcons";
import { Section } from "../../common/primitives";
import { Row, Switch } from "../../common/ui";
import { timezoneChoices, timezoneCity, voiceModeChoices } from "./options";

/** This device's zone: what "Auto" means. */
export const BROWSER_TIMEZONE =
  Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago";

/** The Timezone row's value: "Auto · Chicago" when it follows this device, else the city. */
export function timezoneSummary(timezone: string): string {
  const city = timezoneCity(timezone);
  return timezone === BROWSER_TIMEZONE ? `Auto · ${city}` : city;
}

/** One picker: a plain list of choices, the chosen one checked. */
export function ChoiceList<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  /** The accessible name of the group. */
  label: string;
  options: Array<{
    value: T;
    label: string;
    subtitle?: string;
    icon?: ReactNode;
  }>;
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="ui-list" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <Row
          key={o.value}
          icon={o.icon}
          title={o.label}
          subtitle={o.subtitle}
          selected={o.value === value}
          onClick={() => onChange(o.value)}
        />
      ))}
    </div>
  );
}

/** Coding agent: the harnesses from `/config`, in the server's order. */
export function HarnessPicker({
  harnesses,
  onChange,
}: {
  harnesses: HarnessInfo[];
  onChange: (name: string) => void;
}) {
  return (
    <ChoiceList
      label="Coding agent"
      options={harnesses.map((h) => ({
        value: h.name,
        label: h.display_name,
        icon: <HarnessBrandIcon icon={h.icon} size={18} />,
      }))}
      value={harnesses.find((h) => h.is_default)?.name ?? ""}
      onChange={onChange}
    />
  );
}

export function VoicePicker({
  prefs,
  onUpdate,
}: {
  prefs: RuntimePreferences;
  onUpdate: (patch: PreferencesPatch) => void;
}) {
  return (
    <ChoiceList
      label="Voice"
      options={prefs.available_voices.map((name) => ({
        value: name,
        label: name,
      }))}
      value={prefs.voice_name}
      onChange={(voice_name) => onUpdate({ voice_name })}
    />
  );
}

/** Voice mode: native or cascaded, from the next conversation. */
export function VoiceModePicker({
  prefs,
  onUpdate,
}: {
  prefs: RuntimePreferences;
  onUpdate: (patch: PreferencesPatch) => void;
}) {
  return (
    <ChoiceList
      label="Voice mode"
      options={voiceModeChoices(prefs)}
      value={prefs.voice_mode}
      onChange={(voice_mode) => onUpdate({ voice_mode })}
    />
  );
}

/** An Auto switch (follow this device), then, when it is off, the zones to pick from. */
export function TimezonePicker({
  prefs,
  onUpdate,
}: {
  prefs: RuntimePreferences;
  onUpdate: (patch: PreferencesPatch) => void;
}) {
  const [manual, setManual] = useState(prefs.timezone !== BROWSER_TIMEZONE);
  const setAuto = (auto: boolean) => {
    setManual(!auto);
    if (auto) onUpdate({ timezone: BROWSER_TIMEZONE });
  };
  return (
    <>
      <div className="ui-list">
        <Row
          title="Set automatically"
          subtitle={`${timezoneCity(BROWSER_TIMEZONE)} · ${
            prefs.local_clock_short || prefs.tz_abbrev
          }`}
          trailing={
            <Switch
              checked={!manual}
              onChange={setAuto}
              label="Set timezone automatically"
            />
          }
        />
      </div>
      {manual && (
        <Section label="Timezone">
          <ChoiceList
            label="Timezone"
            options={timezoneChoices(BROWSER_TIMEZONE, prefs).map((tz) => ({
              value: tz,
              label: timezoneCity(tz),
              subtitle: tz.replace(/_/g, " "),
            }))}
            value={prefs.timezone}
            onChange={(timezone) => onUpdate({ timezone })}
          />
        </Section>
      )}
    </>
  );
}
