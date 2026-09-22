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

import { memo } from "react";
import { RoutinesPanel } from "@/components/panels/routines-panel";

// Thin wrapper kept as the Scheduled section's single entry point: this used
// to also render RemindersPanel before the reminder capability was removed.
function ScheduledPanelImpl() {
  return <RoutinesPanel />;
}

export const ScheduledPanel = memo(ScheduledPanelImpl);
