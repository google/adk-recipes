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

import type { TrajectoryRecord } from "./types";

export interface DetailTab {
  id: "preview" | "tools" | "raw";
  label: string;
}

export function tabsFor(record: TrajectoryRecord): DetailTab[] {
  const tabs: DetailTab[] = [];
  if (record.kind !== "tool") {
    tabs.push({ id: "preview", label: "Preview" });
  }
  if (record.kind === "tool" || record.toolName) {
    tabs.push({ id: "tools", label: "Tools" });
  }
  tabs.push({ id: "raw", label: "Raw" });
  return tabs;
}

export function defaultTabFor(
  record: TrajectoryRecord,
): "preview" | "tools" | "raw" {
  return record.kind === "tool" ? "tools" : "preview";
}
