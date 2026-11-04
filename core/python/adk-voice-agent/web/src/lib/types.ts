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

import type { AnswerCardData } from "./cards";

export type ThreadState =
  | "executing"
  | "awaiting_permission"
  | "completed"
  | "failed"
  | "cancelled";

export type ThreadStatus = "running" | "awaiting_approval" | "idle";
export type ThreadOutcome = "finished" | "failed" | "cancelled" | "";

export interface ThreadPermission {
  action_class: string;
  command: string;
  key: string;
  kind?: "tool" | "shell" | string;
  arguments?: Record<string, unknown>;
  summary?: string;
}

export type PermissionRequest = ThreadPermission;

export interface ThreadOutputLink {
  kind:
    | "pr"
    | "branch"
    | "doc"
    | "sheet"
    | "slides"
    | "file"
    | "event"
    | "draft";
  /** "" when the app didn't name it (an edited Doc): show the kind instead. */
  title: string;
  url: string;
}

export interface ThreadEvent {
  seq: number;
  kind: string;
  message: string;
  ts: number;
}

export interface ThreadTurn {
  seq: number;
  prompt: string;
  outcome: "running" | "finished" | "failed" | "cancelled";
  answer: string;
  summary: string;
  error: string | null;
  cards: AnswerCardData[];
  links: ThreadOutputLink[];
  started_at: number;
  ended_at: number | null;
  elapsed_seconds: number;
  events: ThreadEvent[];
}

export interface ThreadSummary {
  thread_id: string;
  title: string;
  goal: string;
  /** Folder relative to home: `code/<repo>`, `projects/<name>`, `scratch/<name>`. */
  project: string;
  state: ThreadState;
  status: ThreadStatus;
  outcome: ThreadOutcome;
  is_running: boolean;
  is_gate: boolean;
  is_finished: boolean;
  is_stale: boolean;
  turn_count: number;
  latest_update: string;
  result_text: string;
  summary: string;
  error: string | null;
  permission: ThreadPermission | null;
  links: ThreadOutputLink[];
  cards: AnswerCardData[];
  created_at: number;
  updated_at: number;
  ended_at: number | null;
  elapsed_seconds: number;
  last_seq: number;
}

export interface ThreadDetail extends ThreadSummary {
  turns: ThreadTurn[];
  events: ThreadEvent[];
}

export interface HarnessInfo {
  name: string;
  display_name: string;
  icon: string;
  is_default: boolean;
}

/** Connecting is the permission: there are no per-service switches. */
export interface AppInfo {
  name: string;
  display_name: string;
  icon: string;
  connected: boolean;
  account_label: string | null;
  /** Connected, but missing access the app now asks for: offer Grant access. */
  needs_grant: boolean;
  services: string[];
  manage_url: string | null;
}

export type VoiceMode = "native" | "cascaded";

export interface RuntimePreferences {
  timezone: string;
  tz_abbrev: string;
  local_clock_short: string;
  voice_name: string;
  voice_mode: VoiceMode;
  save_to_drive: "on" | "off";
  available_voices: string[];
  available_voice_modes: VoiceMode[];
  common_timezones: string[];
}

export type PreferencesPatch = Partial<
  Pick<
    RuntimePreferences,
    "timezone" | "voice_name" | "voice_mode" | "save_to_drive"
  >
>;

export type A2UISurfaceKind = "context_card";

// biome-ignore lint/suspicious/noExplicitAny: free-form tool results; each per-app card reads only the fields it knows.
export type LooseRecord = Record<string, any>;

export interface ContextModel {
  brandIcon: string;
  bullets: string[];
  items: LooseRecord[];
  meta: LooseRecord;
}

export interface A2UIDataModel {
  context?: ContextModel;
}

export interface A2UISurface {
  surfaceId: string;
  kind: A2UISurfaceKind;
  subkind?: string;
  slot?: "stage" | "dock";
  harness?: string;
  title: string;
  subtitle: string;
  dataModel: A2UIDataModel;
}

export interface ComputerRecord {
  state:
    | "starting"
    | "running"
    | "stopping"
    | "stopped"
    | "missing"
    | "error"
    | null;
}

export interface OrchestratorState {
  threads: ThreadSummary[];
  a2ui_surfaces: A2UISurface[];
  computer?: ComputerRecord;
}

export interface ProjectSummary {
  id: string;
  kind: "project" | "code" | "scratch";
  title: string;
  status_line: string;
  last_activity_at: string;
  needs_you: number;
  running: number;
  /** A stable hue (0–359) for the project; the UI maps it onto its tokens. */
  tint: number;
}

export interface ProjectList {
  projects: ProjectSummary[];
}

export interface ProjectThread {
  project: ProjectSummary;
  threads: ThreadSummary[];
}

export interface Me {
  user_id: string;
}

export interface AccountSummary {
  user_id: string;
  /** IAP's sign-out URL when deployed; null locally (nothing to sign out of). */
  sign_out_url: string | null;
}

export interface OrchestratorConfig {
  account: AccountSummary;
  harnesses: HarnessInfo[];
  apps: AppInfo[];
  preferences: RuntimePreferences;
  memory_enabled: boolean;
}

export interface EpisodeMemory {
  id: string;
  kind: "conversation" | "thread";
  fact: string;
}

export interface EpisodeMemories {
  conversation: EpisodeMemory[];
  thread: EpisodeMemory[];
}

export interface MemoryField {
  id: string;
  field: string;
  label: string;
  value: string;
}
