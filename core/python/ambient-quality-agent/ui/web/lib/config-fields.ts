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

/** How the effective configuration is grouped, named and typed for reading.
 *
 * The payload is a flat bag of snake_case keys in whatever order the agent
 * serialised them, which is the order of a Python dataclass rather than the
 * order anyone thinks about a deployment in. These tables turn it into
 * sections someone can scan.
 */

/** Field groups, in display order, and the keys in each. Anything the agent
 *  returns that is not listed here lands in "Other", so a newly added config
 *  field shows up on its own rather than disappearing — the grouping must not
 *  be able to hide a field by not knowing about it. */
export const CONFIG_GROUPS: ReadonlyArray<
  readonly [string, readonly string[]]
> = [
  ["Observed agent", ["observed_agent_name", "project_id", "location"]],
  [
    "Evaluation",
    [
      "multi_turn_metrics",
      "single_turn_metrics",
      "data_lookback_window",
      "data_evaluation_cap",
      "sync_investigation",
    ],
  ],
  [
    "Ambient triggers",
    [
      "ambient_cadence_seconds",
      "agent_revision_trigger_delay_seconds",
      "delay_task_queue",
      "ambient_caller_sa",
    ],
  ],
  [
    "Telemetry source",
    [
      "telemetry_ingestion_source",
      "telemetry_dataset",
      "telemetry_table",
      "telemetry_location",
    ],
  ],
  [
    "AQuA storage",
    [
      "aqa_engine_id",
      "aqa_dataset",
      "aqa_dataset_location",
      "jobs_gcs_bucket",
      "insights_auto_resolve_days",
    ],
  ],
  ["Models", ["base_model", "insights_model", "insights_match_model"]],
];

/** Keys whose raw name reads badly or ambiguously. Everything else falls back
 *  to the key with its underscores opened out, which is legible enough that
 *  naming all of them would be churn. */
export const CONFIG_LABELS: Readonly<Record<string, string>> = {
  observed_agent_name: "Observed agent",
  project_id: "Project",
  location: "Region",
  aqa_engine_id: "AQuA's own engine id",
  multi_turn_metrics: "Multi-turn metrics",
  single_turn_metrics: "Single-turn metrics",
  data_lookback_window: "Lookback window (days)",
  data_evaluation_cap: "Evaluation cap",
  sync_investigation: "Run synchronously",
  ambient_cadence_seconds: "Cadence (seconds)",
  agent_revision_trigger_delay_seconds: "Revision trigger delay (seconds)",
  delay_task_queue: "Delay task queue",
  ambient_caller_sa: "Ambient caller SA",
  telemetry_ingestion_source: "Source",
  insights_auto_resolve_days: "Auto-resolve insights (days)",
};

export function configLabel(key: string): string {
  return CONFIG_LABELS[key] || key.replace(/_/g, " ");
}

/** One group of the config as it will be drawn: its title and the entries
 *  actually present under it. */
export interface ConfigGroup {
  title: string;
  entries: Array<[string, unknown]>;
}

/**
 * The config, split into its curated groups plus "Other".
 *
 * Presence is tested with `in`, not truthiness, so an explicit `null` is a
 * rendered row: "this is unset" is usually the answer someone opened this card
 * to get, and dropping the key would leave them unable to tell it from a key
 * that does not exist. A group with nothing present renders no heading at all.
 */
export function groupConfig(config: Record<string, unknown>): ConfigGroup[] {
  const seen = new Set<string>();
  const groups: ConfigGroup[] = [];

  for (const [title, keys] of CONFIG_GROUPS) {
    const entries: Array<[string, unknown]> = [];
    for (const key of keys) {
      if (!(key in config)) continue;
      seen.add(key);
      entries.push([key, config[key]]);
    }
    if (entries.length) groups.push({ title, entries });
  }

  const rest = Object.keys(config).filter((k) => !seen.has(k));
  if (rest.length) {
    groups.push({
      title: "Other",
      entries: rest.map((key) => [key, config[key]] as [string, unknown]),
    });
  }

  return groups;
}

/** A config value rendered as text, or as the pills a list wants.
 *
 * `0` and `false` are values, not absences: a cadence of 0 and an unset cadence
 * are different deployments, and collapsing them into one em dash is how a
 * dashboard tells someone their config is empty when it is not.
 */
export type ConfigValue =
  | { kind: "text"; text: string }
  | { kind: "pills"; items: string[] };

export function configValue(value: unknown): ConfigValue {
  if (value === null || value === undefined || value === "") {
    return { kind: "text", text: "—" };
  }
  if (typeof value === "boolean") {
    return { kind: "text", text: value ? "yes" : "no" };
  }
  if (Array.isArray(value)) {
    if (!value.length) return { kind: "text", text: "none" };
    return { kind: "pills", items: value.map((v) => String(v)) };
  }
  if (typeof value === "object") {
    const o = value as Record<string, unknown>;
    // A model record: "gemini-3.5-flash (global)".
    if (o.model) {
      return {
        kind: "text",
        text: o.location
          ? `${String(o.model)} (${String(o.location)})`
          : String(o.model),
      };
    }
    return { kind: "text", text: JSON.stringify(value) };
  }
  return { kind: "text", text: String(value) };
}
