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


import { useState } from "react";
import { ChevronRight, Loader2, Mail } from "lucide-react";
import {
  useGoogleConnection,
  connectGoogle,
  disconnectGoogle,
  isExpired,
} from "@/lib/gcp-connection";

// Keys must mirror WORKSPACE_SURFACES in app/auth/oauth.py — the
// backend drives the available list; an unmapped key falls back to its raw name.
const SURFACE_LABELS: Record<string, string> = {
  drive: "Drive",
  gmail: "Gmail",
  calendar: "Calendar",
  sheets: "Sheets",
  docs: "Docs",
  chat: "Chat",
  tasks: "Tasks",
  slides: "Slides",
  keep: "Keep",
  script: "Apps Script",
  meet: "Meet",
  forms: "Forms",
  directory: "Directory",
};

function expiresLabel(expiresAt: number | null | undefined): string {
  if (!expiresAt) return "";
  const mins = Math.max(0, Math.round((expiresAt * 1000 - Date.now()) / 60000));
  return ` · expires in ~${mins}m`;
}

export function WorkspaceConnectCard() {
  const { status, refresh } = useGoogleConnection();
  const ws = status?.workspace;
  const available = status?.surfaces ?? Object.keys(SURFACE_LABELS);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [readonly, setReadonly] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const allSelected =
    available.length > 0 && available.every((s) => selected.has(s));

  function toggle(surface: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(surface)) next.delete(surface);
      else next.add(surface);
      return next;
    });
  }

  async function run(fn: () => Promise<void>) {
    setErr(null);
    setBusy(true);
    try {
      await fn();
      for (const d of [3000, 6000, 10000]) setTimeout(() => void refresh(), d);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded border border-border p-2 text-xs">
      <div className="flex items-center gap-2 font-medium text-foreground">
        <Mail className="h-4 w-4 text-primary" />
        Google Workspace
      </div>

      {ws?.connected ? (
        <>
          {isExpired(ws.expires_at) ? (
            <div className="text-destructive">Connection expired — reconnect</div>
          ) : (
            <div className="text-muted-foreground">
              Connected · {ws.readonly ? "read-only" : "read-write"}
              {ws.surfaces.length
                ? ` · ${ws.surfaces.map((s) => SURFACE_LABELS[s] ?? s).join(", ")}`
                : ""}
              {expiresLabel(ws.expires_at)}
            </div>
          )}
          <div className="flex gap-2">
            <button
              onClick={() =>
                run(() =>
                  connectGoogle("workspace", {
                    surfaces: ws.surfaces,
                    readonly: ws.readonly,
                  }),
                )
              }
              disabled={busy || ws.surfaces.length === 0}
              className="rounded border border-border px-2 py-1 hover:bg-muted disabled:opacity-50"
            >
              Reconnect
            </button>
            <button
              onClick={() => run(() => disconnectGoogle("workspace").then(refresh))}
              disabled={busy}
              className="rounded border border-border px-2 py-1 hover:bg-muted disabled:opacity-50"
            >
              Disconnect
            </button>
          </div>
        </>
      ) : null}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="flex flex-1 items-center gap-1 text-muted-foreground hover:text-foreground"
        >
          <ChevronRight
            className={`h-3.5 w-3.5 shrink-0 transition-transform ${
              expanded ? "rotate-90" : ""
            }`}
          />
          Scopes ·{" "}
          {allSelected ? (
            <span className="font-medium text-primary">
              all {available.length} selected
            </span>
          ) : (
            `${selected.size} selected`
          )}
        </button>
        <button
          type="button"
          onClick={() => setSelected(new Set(available))}
          aria-pressed={allSelected}
          className={`rounded border px-1.5 py-0.5 transition-colors ${
            allSelected
              ? "border-primary bg-primary text-primary-foreground hover:bg-primary/90"
              : "border-border hover:bg-muted"
          }`}
        >
          All
        </button>
        <button
          type="button"
          onClick={() => setSelected(new Set())}
          aria-pressed={selected.size === 0}
          className={`rounded border px-1.5 py-0.5 transition-colors ${
            selected.size === 0
              ? "border-primary bg-primary text-primary-foreground hover:bg-primary/90"
              : "border-border hover:bg-muted"
          }`}
        >
          None
        </button>
      </div>
      {expanded ? (
        <div className="flex flex-wrap gap-1.5">
          {available.map((s) => {
            const on = selected.has(s);
            return (
              <button
                key={s}
                type="button"
                onClick={() => toggle(s)}
                aria-pressed={on}
                className={`rounded-full border px-2 py-0.5 transition-colors ${
                  on
                    ? "border-primary bg-primary text-primary-foreground hover:bg-primary/90"
                    : "border-border text-muted-foreground hover:bg-muted"
                }`}
              >
                {SURFACE_LABELS[s] ?? s}
              </button>
            );
          })}
        </div>
      ) : null}
      {selected.has("chat") && !selected.has("directory") ? (
        <div className="text-[10px] text-primary">
          Tip: also tick Directory to resolve Chat sender names instead of raw
          user IDs.
        </div>
      ) : null}
      <label className="flex items-center gap-1.5 text-muted-foreground">
        <input
          type="checkbox"
          checked={readonly}
          onChange={(e) => setReadonly(e.target.checked)}
        />
        Read-only
      </label>
      <button
        onClick={() =>
          run(() =>
            connectGoogle("workspace", {
              surfaces: [...selected],
              readonly,
            }),
          )
        }
        disabled={busy || selected.size === 0}
        className="inline-flex w-fit items-center gap-1.5 rounded bg-primary px-2.5 py-1 font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        {ws?.connected ? "Reconnect Workspace" : "Connect Workspace"}
      </button>

      <div className="text-[10px] text-muted-foreground">
        Short-lived token (~1h) for gws; pick surfaces + read-only. Reconnect to
        change scopes or when it lapses.
      </div>
      {err ? <div className="text-destructive">{err}</div> : null}
    </div>
  );
}
