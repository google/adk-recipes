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


import { useState, type ReactNode } from "react";
import {
  Activity,
  Brain,
  Sparkles,
  GitBranch,
  KeyRound,
  Cloud,
  Clock,
  Terminal,
  ChevronRight,
  type LucideIcon,
} from "lucide-react";
import { useLhaState, type HorizonStateResponse } from "@/lib/horizon-state";
import { useLhaMemories } from "@/lib/horizon-memories";
import { useLhaSecrets } from "@/lib/horizon-secrets";
import { useGoogleConnection } from "@/lib/gcp-connection";
import { cn } from "@/lib/utils";
import { SkillsPanel } from "@/components/panels/skills-panel";
import { MemoryPanel, MEMORY_PANEL_PAGE_LIMIT } from "@/components/panels/memory-panel";
import { AuthPanel } from "@/components/panels/auth-panel";
import { SecretsPanel } from "@/components/panels/secrets-panel";
import { useRoutines } from "@/lib/horizon-routines";
import { ScheduledPanel } from "@/components/panels/scheduled-panel";
import { DelegationTree } from "@/components/panels/delegation-tree";
import { ActivityPanel } from "@/components/panels/activity-panel";
import { BackgroundPanel } from "@/components/panels/background-panel";
import { useProcesses } from "@/lib/horizon-processes";

interface SidePanelsProps {
  contextId: string | null;
}

export const selectCounts = (d: HorizonStateResponse) => ({
  activity: d.state.tool_call_log?.length ?? 0,
  skills: d.state.bound_skills?.length ?? 0,
  delegation: d.state.delegate_history?.length ?? 0,
});

export function SidePanels({ contextId }: SidePanelsProps) {
  const { data: counts } = useLhaState(contextId, { select: selectCounts });
  // Share the query cache key with MemoryPanel so the badge total and the
  // panel pills come from a single fetch.
  const { data: memories } = useLhaMemories(MEMORY_PANEL_PAGE_LIMIT);
  const { data: secrets } = useLhaSecrets();
  const { data: routines } = useRoutines();
  const { status: googleStatus } = useGoogleConnection();
  const c = counts ?? { activity: 0, skills: 0, delegation: 0 };
  const memoryCount = memories
    ? memories.counts.user + memories.counts.agent + memories.counts.user_profile
    : 0;
  const secretsCount = secrets?.secrets.length ?? 0;
  const authCount =
    (googleStatus?.gcp.connected ? 1 : 0) +
    (googleStatus?.workspace.connected ? 1 : 0);
  const routinesCount = routines?.routines.length ?? 0;
  const { data: processes } = useProcesses();
  const runningCount = processes?.processes.filter((p) => p.running).length ?? 0;

  const authConnected = authCount > 0;

  return (
    <div className="flex flex-col gap-1">
      <Section label="Activity" Icon={Activity} count={c.activity} defaultOpen>
        <ActivityPanel contextId={contextId} />
      </Section>
      <Section label="Background" Icon={Terminal} count={runningCount}>
        <BackgroundPanel />
      </Section>
      <Section label="Memory" Icon={Brain} count={memoryCount}>
        <MemoryPanel contextId={contextId} />
      </Section>
      <Section
        label="Auth"
        Icon={Cloud}
        count={authCount}
        badge={
          authConnected ? (
            <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0 font-mono text-[10px] text-emerald-500">
              {authCount}
            </span>
          ) : undefined
        }
      >
        <AuthPanel />
      </Section>
      <Section label="Secrets" Icon={KeyRound} count={secretsCount}>
        <SecretsPanel />
      </Section>
      <Section label="Scheduled" Icon={Clock} count={routinesCount}>
        <ScheduledPanel />
      </Section>
      <Section label="Skills" Icon={Sparkles} count={c.skills}>
        <SkillsPanel contextId={contextId} />
      </Section>
      <Section label="Agents" Icon={GitBranch} count={c.delegation}>
        <DelegationTree contextId={contextId} />
      </Section>
    </div>
  );
}

interface SectionProps {
  label: string;
  Icon: LucideIcon;
  count: number;
  defaultOpen?: boolean;
  badge?: ReactNode;
  children: ReactNode;
}

export function Section({ label, Icon, count, defaultOpen = false, badge, children }: SectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-md">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70" />
        <span className="flex-1 text-xs font-medium text-muted-foreground">
          {label}
        </span>
        {badge ?? (
          <span
            className={cn(
              "font-mono text-[10px] tabular-nums",
              count > 0 ? "text-muted-foreground/70" : "text-muted-foreground/30",
            )}
          >
            {count}
          </span>
        )}
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground/40 transition-transform group-hover:text-muted-foreground/70",
            open && "rotate-90",
          )}
        />
      </button>
      {open ? (
        <div className="mt-1 max-h-96 overflow-y-auto pb-2">{children}</div>
      ) : null}
    </div>
  );
}
