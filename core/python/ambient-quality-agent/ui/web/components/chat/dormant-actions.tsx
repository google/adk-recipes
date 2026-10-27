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

import {
  Activity,
  ArrowRight,
  ListChecks,
  type LucideIcon,
} from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";
import { useNow } from "@/lib/now-context";

// A session is "dormant" once it has had no activity for longer than this.
export const DORMANT_THRESHOLD_MS = 8 * 60 * 60 * 1000;

export function isDormant(
  lastUpdated: number | null | undefined,
  now: number,
): boolean {
  if (lastUpdated == null) return false;
  return now - lastUpdated > DORMANT_THRESHOLD_MS;
}

interface DormantAction {
  label: string;
  prompt: string;
  Icon: LucideIcon;
}

const DORMANT_ACTIONS: DormantAction[] = [
  {
    label: "Status check",
    prompt:
      "Give me a status check: what's been done so far and where do things stand right now?",
    Icon: Activity,
  },
  {
    label: "Continue next step",
    prompt: "Continue with the next step.",
    Icon: ArrowRight,
  },
  {
    label: "Remind me the plan",
    prompt:
      "Remind me of the plan: what's the overall goal and which steps are still remaining?",
    Icon: ListChecks,
  },
];

export interface DormantActionsProps {
  onAction: (prompt: string) => void;
  disabled?: boolean;
  className?: string;
}

export function DormantActions({
  onAction,
  disabled,
  className,
}: DormantActionsProps) {
  return (
    <div
      className={cn(
        "mx-auto flex w-full max-w-2xl flex-wrap items-center gap-2 px-4 pb-1 pt-2",
        className,
      )}
    >
      <span className={textStyle.label}>Pick up where you left off</span>
      {DORMANT_ACTIONS.map(({ label, prompt, Icon }) => (
        <button
          key={label}
          type="button"
          disabled={disabled}
          onClick={() => onAction(prompt)}
          className={cn(
            textStyle.meta,
            "group/dormant inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 shadow-sm transition-all hover:-translate-y-0.5 hover:border-primary/40 hover:bg-primary/5 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0",
          )}
        >
          <Icon className="h-3.5 w-3.5 text-primary/70 transition-colors group-hover/dormant:text-primary" />
          {label}
        </button>
      ))}
    </div>
  );
}

export interface DormantActionsGateProps {
  lastUpdated: number | null;
  show: boolean;
  disabled?: boolean;
  onAction: (prompt: string) => void;
}

export function DormantActionsGate({
  lastUpdated,
  show,
  disabled,
  onAction,
}: DormantActionsGateProps) {
  const now = useNow();
  if (!show || !isDormant(lastUpdated, now)) return null;
  return <DormantActions onAction={onAction} disabled={disabled} />;
}
