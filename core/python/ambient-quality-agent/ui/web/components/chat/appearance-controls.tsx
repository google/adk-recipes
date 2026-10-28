"use client";
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

import { Monitor, Moon, Sun } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import {
  SKINS,
  THEME_CHOICES,
  type Skin,
  type ThemeChoice,
} from "@/lib/use-appearance";

const THEME_LABELS: Record<ThemeChoice, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};

const THEME_ICONS: Record<ThemeChoice, LucideIcon> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

// Three states need three controls. The toggle this replaced could only ever
// express two, so "follow the system" had nowhere to live.
const THEME_TITLES: Record<ThemeChoice, string> = {
  system: "Match your system setting",
  light: "Always light",
  dark: "Always dark",
};

const SKIN_LABELS: Record<Skin, string> = {
  default: "Default",
  ocean: "Ocean",
  ember: "Ember",
  moss: "Moss",
};

// Preview swatch color per skin — mirrors the --primary set in globals.css so
// the picker reads as the accent it applies.
const SKIN_SWATCH: Record<Skin, string> = {
  default: "hsl(220 16% 42%)",
  ocean: "hsl(205 90% 48%)",
  ember: "hsl(18 85% 52%)",
  moss: "hsl(152 48% 38%)",
};

export function AppearanceControls({
  themeChoice,
  onThemeChange,
  skin,
  onSkinChange,
}: {
  themeChoice: ThemeChoice;
  onThemeChange: (choice: ThemeChoice) => void;
  skin: Skin;
  onSkinChange: (skin: Skin) => void;
}) {
  return (
    <div className="flex w-56 flex-col gap-4">
      <section className="flex flex-col gap-2">
        <h3 className={textStyle.label}>Theme</h3>
        <div className="flex items-stretch overflow-hidden rounded-md border">
          {THEME_CHOICES.map((c) => {
            const Icon = THEME_ICONS[c];
            return (
              <button
                key={c}
                type="button"
                onClick={() => onThemeChange(c)}
                aria-label={`${THEME_LABELS[c]} theme`}
                aria-pressed={themeChoice === c}
                title={THEME_TITLES[c]}
                className={cn(
                  textStyle.meta,
                  "flex flex-1 items-center justify-center gap-1.5 border-r px-2 py-1.5 last:border-r-0",
                  themeChoice === c
                    ? "bg-primary text-primary-foreground"
                    : "lh-sidebar-hover text-foreground",
                )}
              >
                <Icon className="h-4 w-4" />
                <span>{THEME_LABELS[c]}</span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className={textStyle.label}>Accent</h3>
        <div className="flex items-center gap-2">
          {SKINS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onSkinChange(s)}
              aria-label={`${SKIN_LABELS[s]} accent`}
              aria-pressed={skin === s}
              title={SKIN_LABELS[s]}
              className={cn(
                "h-6 w-6 rounded-full border transition-transform hover:scale-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                skin === s ? "ring-2 ring-ring ring-offset-2" : "ring-0",
              )}
              style={{ backgroundColor: SKIN_SWATCH[s] }}
            />
          ))}
        </div>
      </section>
    </div>
  );
}
