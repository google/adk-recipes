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

// A new project-chat's workspace_window, keyed by its pre-generated contextId.
// The composer reads (and consumes) it on the first send to seed the window
// before the session's first turn — so no empty session lingers on navigation.
const pending = new Map<string, string[]>();

export function setPendingWindow(contextId: string, dirs: string[]): void {
  pending.set(contextId, dirs);
}

export function takePendingWindow(contextId: string): string[] | undefined {
  const dirs = pending.get(contextId);
  if (dirs !== undefined) pending.delete(contextId);
  return dirs;
}
