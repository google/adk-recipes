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
 * Text roles for the dashboard. Every element that sets a font size uses one of
 * these. Choose by what the text is: the page's title or a headline figure is
 * pageTitle; a section, card or dialog title is sectionTitle; the name of a row
 * or item is itemTitle; the name of a control, navigation group, column, figure
 * or status is label; code or tool output is code; secondary text is
 * description (under a title) or meta; anything else is body.
 *
 * Sizes and line heights come from the theme in tailwind.config.ts. Each role
 * states its weight, optical size and color, so it looks the same whatever it
 * is nested in: opsz-display picks the Google Sans title design, and opsz-text
 * picks Google Sans Text, drawn for small sizes.
 *
 * A component may change a role's weight (font-normal or font-medium) or its
 * color, never its size: to make text more important, use a higher role.
 */
export const textStyle = {
  pageTitle: "text-xl font-medium opsz-display text-foreground",
  sectionTitle: "text-base font-medium opsz-text text-foreground",
  itemTitle: "text-sm font-medium opsz-text text-foreground",
  body: "text-sm font-normal opsz-text text-foreground",
  description: "text-sm font-normal opsz-text text-muted-foreground",
  meta: "text-xs font-normal opsz-text text-muted-foreground",
  label: "text-xs font-medium opsz-text text-muted-foreground",
  code: "font-mono text-xs font-normal text-foreground",
} as const;

export type TextRole = keyof typeof textStyle;

/** Machine text inside another role: changes the family only. */
export const mono = "font-mono";

const focusRing =
  "rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export const link = {
  /** Inside running text: underlined at rest. */
  inline: `text-primary underline underline-offset-2 hover:decoration-2 ${focusRing}`,
  /** Standing alone: underlined on hover and focus; color comes from the role. */
  standalone: `underline-offset-2 hover:underline focus-visible:underline ${focusRing}`,
} as const;
