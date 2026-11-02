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

// The settings of each page (studio/patches.py SETTING_KEYS): shown at once, saved as actions
// (text after a pause in typing).
import { ChevronIcon, SparkleIcon, VideoIcon } from "../../components/icons";
import { Select, Switch, TextField } from "../../components/RunControls";
import { CardHeader } from "./ItemSlot";
import { RUN_OPTIONS, variationsOf } from "./model";
import type { Studio } from "./useStudio";

const TYPING_MS = 1500;

type Props = { studio: Studio };

const text = (v: unknown) => (v === undefined || v === null ? "" : String(v));

/** Photo Try-On style guidelines and scenario. */
export function StyleFields({ studio }: Props) {
  const settings = studio.view.settings.tryon;
  const set = (key: string) => (value: string) =>
    studio.setSetting("tryon", key, value, TYPING_MS);
  return (
    <div className="glass-panel studio-card">
      <CardHeader
        icon={SparkleIcon}
        title="Style"
        hint="Optional: how the look is worn and where the photo is set."
      />
      <TextField
        id="studio-style"
        label="Style guidelines"
        rows={2}
        value={text(settings.style_guidelines)}
        onChange={set("style_guidelines")}
        placeholder="Brand rules, e.g. jackets closed, shirts tucked"
      />
      <TextField
        id="studio-scenario"
        label="Scenario"
        value={text(settings.scenario)}
        onChange={set("scenario")}
        placeholder="a plain light grey studio environment"
      />
    </div>
  );
}

/** The reference model's options (Photo Try-On settings, also used by Video Try-On from photos). */
export function LookOptions({ studio }: Props) {
  const { complete_look, upscale_crops } = studio.view.settings.tryon;
  return (
    <div className="flex flex-col gap-2">
      <Switch
        label="Complete the look"
        checked={complete_look !== false}
        onChange={(on) => studio.setSetting("tryon", "complete_look", on)}
        hint="Also dress the person in what only the reference model wears (e.g. its skirt and shoes), taken from its photo."
      />
      <Switch
        label="Upscale look-photo crops"
        checked={upscale_crops === true}
        onChange={(on) => studio.setSetting("tryon", "upscale_crops", on)}
        hint="Sharpen the shoes taken from the reference model photo before use. Slower."
      />
    </div>
  );
}

const MOTIONS = [
  ["auto", "Automatic", "Planned from your images"],
  ["custom", "Custom", "Describe the video"],
] as const;

/** Video Try-On motion: automatic, or a custom prompt. */
export function MotionField({ studio }: Props) {
  const { motion, prompt } = studio.view.settings.video_tryon;
  return (
    <div className="glass-panel studio-card">
      <CardHeader icon={VideoIcon} title="Motion" />
      <div
        role="radiogroup"
        aria-label="Motion"
        className="grid grid-cols-2 gap-2"
      >
        {MOTIONS.map(([value, label, hint]) => (
          // biome-ignore lint/a11y/useSemanticElements: a styled button with role="radio" in a radiogroup
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={motion === value}
            onClick={() => studio.setSetting("video_tryon", "motion", value)}
            className={`rounded-xl border p-3 text-left transition-all ${
              motion === value
                ? "border-gm-accent/60 bg-gm-accent/10 shadow-focus"
                : "border-white/10 bg-white/[0.02] hover:border-gm-accent/30"
            }`}
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              <span
                aria-hidden="true"
                className={`w-3.5 h-3.5 rounded-full border-2 ${motion === value ? "border-gm-accent bg-gm-accent shadow-[inset_0_0_0_2px_#050505]" : "border-white/30"}`}
              />
              {label}
            </span>
            <span className="block mt-1 text-caption font-normal text-gm-text-tertiary">
              {hint}
            </span>
          </button>
        ))}
      </div>
      {motion === "custom" && (
        <TextField
          id="studio-prompt"
          label="What the video should show"
          rows={2}
          value={text(prompt)}
          onChange={(value) =>
            studio.setSetting("video_tryon", "prompt", value, TYPING_MS)
          }
          placeholder="e.g. walks to the camera, stops and turns"
        />
      )}
    </div>
  );
}

/** Variations, aspect ratio and image size or resolution of the current page */
export function RunOptionsPanel({ studio }: Props) {
  const cap = studio.view.capability;
  const settings = studio.view.settings[cap];
  const variations = variationsOf(studio.view);
  // The closed panel still says what will run, e.g. "1× · 3:4 · 2K".
  const summary = RUN_OPTIONS[cap]
    .map(
      ({ key }) => text(settings[key]) + (key === "num_variations" ? "×" : ""),
    )
    .filter(Boolean)
    .join(" · ");
  return (
    <details className="group glass-panel rounded-2xl">
      <summary className="flex items-center gap-3 cursor-pointer select-none list-none [&::-webkit-details-marker]:hidden rounded-2xl px-4 sm:px-5 py-4">
        <span className="text-h3">Run options</span>
        <span className="ml-auto text-caption text-gm-text-tertiary tabular-nums">
          {summary}
        </span>
        <ChevronIcon
          size={16}
          className="text-gm-text-secondary transition-transform group-open:rotate-180"
        />
      </summary>
      <div className="grid grid-cols-2 gap-3 px-4 sm:px-5 pb-5 text-sm">
        {RUN_OPTIONS[cap].map(({ key, label, options }) => {
          const value = text(settings[key]);
          // A value the agent set outside the list stays selectable.
          const all = options.includes(value) ? options : [value, ...options];
          const shown = key === "num_variations" ? variations.label : label;
          return (
            <Select
              key={key}
              label={shown}
              value={value}
              options={all}
              onChange={(v) => studio.setSetting(cap, key, v)}
            />
          );
        })}
        {variations.hint && (
          <p className="col-span-2 text-caption text-gm-text-tertiary">
            {variations.hint}
          </p>
        )}
      </div>
    </details>
  );
}
