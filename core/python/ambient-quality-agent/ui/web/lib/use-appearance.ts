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

import { useEffect, useState } from "react";

// What the page is currently painted as. Every consumer that renders against
// the theme -- a syntax highlighter, a toast, a chart -- wants this one.
export type Theme = "light" | "dark";

// What the user asked for, which is not the same thing: "system" resolves to
// either `Theme` depending on the OS, and changes underneath the page when the
// OS does. Keeping the two apart is what makes "follow the system" expressible
// at all -- a single "light" | "dark" cannot record that nothing was chosen.
export type ThemeChoice = "system" | "light" | "dark";

export type Skin = "default" | "ocean" | "ember" | "moss";

export const THEME_CHOICES: ThemeChoice[] = ["system", "light", "dark"];
export const SKINS: Skin[] = ["default", "ocean", "ember", "moss"];

// Unset means "follow the system", as the old dashboard's Auto did. A stored
// "light" or "dark" is an explicit override and survives this default.
export const DEFAULT_THEME_CHOICE: ThemeChoice = "system";

export const DARK_QUERY = "(prefers-color-scheme: dark)";

const THEME_KEY = "lha.theme";
const SKIN_KEY = "lha.skin";

// Must match --background in app/globals.css (:root vs .dark) so the mobile
// browser chrome blends with the page.
const THEME_COLOR: Record<Theme, string> = {
  light: "#fefdfb",
  dark: "#101010",
};

function setItem(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // localStorage unavailable — the change is session-only.
  }
}

// Dark when the browser cannot say -- an old browser, or a test environment
// with no matchMedia -- so an unanswerable question keeps the theme the
// dashboard shipped with rather than silently flipping it to light.
function systemTheme(): Theme {
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

export function resolveTheme(choice: ThemeChoice): Theme {
  return choice === "system" ? systemTheme() : choice;
}

function readThemeChoice(raw: string | null): ThemeChoice {
  return raw === "light" || raw === "dark" || raw === "system"
    ? raw
    : DEFAULT_THEME_CHOICE;
}

function applyTheme(choice: ThemeChoice) {
  const theme = resolveTheme(choice);
  document.documentElement.classList.toggle("dark", theme === "dark");
  const meta = document.getElementById("lha-theme-color");
  if (meta) meta.setAttribute("content", THEME_COLOR[theme]);
}

function applyAttr(name: "skin", value: string, isDefault: boolean) {
  if (isDefault) delete document.documentElement.dataset[name];
  else document.documentElement.dataset[name] = value;
}

function initialTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

// Off storage, not off the `dark` class: the class carries the resolved theme,
// which cannot tell "system, and the OS is dark" from "dark, chosen".
function initialThemeChoice(): ThemeChoice {
  if (typeof window === "undefined") return DEFAULT_THEME_CHOICE;
  try {
    return readThemeChoice(window.localStorage.getItem(THEME_KEY));
  } catch {
    return DEFAULT_THEME_CHOICE;
  }
}

function initialSkin(): Skin {
  if (typeof document === "undefined") return "default";
  const v = document.documentElement.dataset.skin as Skin | undefined;
  return v && SKINS.includes(v) ? v : "default";
}

// Reactively tracks the resolved theme by watching the `dark` class on <html>.
// applyTheme() toggles that class for every theme change — toggle button,
// cross-tab storage sync, and the pre-paint boot script alike — so a consumer
// that only needs to react to the theme (not change it) stays in sync no matter
// which component triggered the switch, without sharing useState.
export function useResolvedTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  useEffect(() => {
    const el = document.documentElement;
    const sync = () =>
      setTheme(el.classList.contains("dark") ? "dark" : "light");
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(el, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

export function useAppearance(): {
  theme: Theme;
  themeChoice: ThemeChoice;
  setTheme: (choice: ThemeChoice) => void;
  skin: Skin;
  setSkin: (skin: Skin) => void;
} {
  const [themeChoice, setThemeChoiceState] =
    useState<ThemeChoice>(initialThemeChoice);
  // Resolved from the choice rather than read off the `dark` class. The class
  // is whatever the boot script painted, and trusting it here would make this
  // hook wrong wherever that script did not run or disagreed -- and would give
  // "system" the OS answer only by way of a coincidence.
  const [theme, setThemeState] = useState<Theme>(() =>
    resolveTheme(initialThemeChoice()),
  );
  const [skin, setSkinState] = useState<Skin>(initialSkin);

  // Repairs any disagreement between the boot script and this model on mount,
  // so the class, the meta tag and `theme` cannot drift apart. Idempotent.
  // biome-ignore lint/correctness/useExhaustiveDependencies(themeChoice): runs on mount only; setTheme applies later choices itself.
  useEffect(() => {
    applyTheme(themeChoice);
  }, []);

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_KEY) {
        // A null newValue is the key cleared in another tab, which is the same
        // state as never having chosen: back to following the system.
        const next = readThemeChoice(e.newValue);
        setThemeChoiceState(next);
        setThemeState(resolveTheme(next));
        applyTheme(next);
      } else if (e.key === SKIN_KEY) {
        const next = (
          e.newValue && SKINS.includes(e.newValue as Skin)
            ? e.newValue
            : "default"
        ) as Skin;
        setSkinState(next);
        applyAttr("skin", next, next === "default");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // The "automatic" half of "system": the OS switching at sunset has to reach a
  // page that is already open, or the setting only means "whatever the OS said
  // when this tab loaded". Subscribed always and gated on the choice inside, so
  // that flipping to system and back does not re-subscribe.
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const query = window.matchMedia(DARK_QUERY);
    const onChange = () => {
      if (themeChoice !== "system") return;
      setThemeState(resolveTheme("system"));
      applyTheme("system");
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [themeChoice]);

  const setTheme = (next: ThemeChoice) => {
    setThemeChoiceState(next);
    setThemeState(resolveTheme(next));
    setItem(THEME_KEY, next);
    applyTheme(next);
  };

  const setSkin = (next: Skin) => {
    setSkinState(next);
    setItem(SKIN_KEY, next);
    applyAttr("skin", next, next === "default");
  };

  return { theme, themeChoice, setTheme, skin, setSkin };
}
