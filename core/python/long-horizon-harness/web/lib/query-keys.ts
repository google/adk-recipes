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

export const qk = {
  state: (contextId?: string) => ["lha", "state", contextId ?? null] as const,
  sessions: (source: "user" | "scheduler", q?: string) =>
    ["lha", "sessions", source, q ?? ""] as const,
  tasks: (contextId: string) => ["lha", "contexts", contextId, "tasks"] as const,
  memories: (limit: number) => ["lha", "memories", limit] as const,
  secrets: () => ["lha", "secrets"] as const,
  routines: () => ["lha", "routines"] as const,
  processes: () => ["lha", "processes"] as const,
  gcpConnection: () => ["lha", "gcp", "status"] as const,
  sandboxStatus: () => ["lha", "sandbox", "status"] as const,
  workspaceFolders: () => ["lha", "workspace", "folders"] as const,
};
