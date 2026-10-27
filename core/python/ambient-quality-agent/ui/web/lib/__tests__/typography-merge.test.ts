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
import { type TextRole, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

const extractSize = (classes: string) =>
  classes.match(/\btext-(xs|sm|base|xl)\b/)?.[0];

describe("cn() with a role", () => {
  it("keeps the size and replaces the color", () => {
    const merged = cn(textStyle.meta, "text-destructive");
    expect(merged).toContain("text-xs");
    expect(merged).toContain("text-destructive");
    expect(merged).not.toContain("text-muted-foreground");
  });

  it("keeps every role's size under a color or weight adjustment", () => {
    for (const role of Object.keys(textStyle) as TextRole[]) {
      const classes = textStyle[role];
      expect(extractSize(classes), role).toBeDefined();
      const merged = cn(classes, "text-primary", "font-normal");
      expect(extractSize(merged), role).toBe(extractSize(classes));
    }
  });
});
