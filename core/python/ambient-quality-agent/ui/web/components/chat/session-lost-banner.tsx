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

import { useEffect } from "react";
import { Loader2 } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

const POLL_INTERVAL_MS = 2000;
const PROBE_URL = "/.well-known/agent-card.json";

export function SessionLostBanner() {
  useEffect(() => {
    let cancelled = false;

    const probe = async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), POLL_INTERVAL_MS);
      try {
        const resp = await fetch(PROBE_URL, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!cancelled && resp.ok) {
          window.location.reload();
        }
      } catch {
        // Backend still down; the interval will retry.
      } finally {
        clearTimeout(timer);
      }
    };

    void probe();
    const id = setInterval(probe, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        textStyle.meta,
        "flex items-center gap-2 border-b border-destructive/40 bg-destructive/10 px-4 py-2 text-destructive",
      )}
    >
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      <span className="font-medium">Chat connection lost — reconnecting…</span>
      <span className="text-destructive/80">
        Will reload automatically when the backend responds.
      </span>
    </div>
  );
}
