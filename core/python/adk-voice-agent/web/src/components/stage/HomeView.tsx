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

import { ChevronRight } from "lucide-react";
import type {
  A2UISurface,
  AppInfo,
  ProjectSummary,
  ThreadSummary,
} from "../../lib/api";
import {
  folderTitle,
  groupHomeThreads,
  sameTitle,
  tintStyle,
} from "../../lib/projects";
import { appToConnect, starterSuggestions } from "../../lib/starters";
import { threadTitle, threadWhen } from "../../lib/threads";
import { A2UISurfaceDeck } from "../a2ui/A2UISurfaceDeck";
import { StarterSuggestions } from "../a2ui/StarterSuggestions";
import { Button } from "../common/ui";
import { ConnectCard } from "./ConnectCard";
import { ThreadMeta } from "./thread/ThreadMeta";
import { ThreadOutputs } from "./thread/ThreadOutputs";
import { type ThreadActions, ThreadReply } from "./thread/ThreadReply";
import { useNow } from "./time";

function ThreadSummaryCard({
  thread,
  project,
  now,
  actions,
  onOpenThread,
  onOpenProject,
}: {
  thread: ThreadSummary;
  project?: ProjectSummary;
  now: number;
  actions: ThreadActions;
  onOpenThread: (thread: ThreadSummary) => void;
  onOpenProject: (projectId: string) => void;
}) {
  const title = threadTitle(thread);
  const projTitle = project?.title ?? folderTitle(thread.project);
  // A project made for this one thread is named after it: say the name once.
  const sameName = sameTitle(projTitle, title);
  const when = threadWhen(thread, now);

  return (
    <article
      className="thread-home-card"
      data-testid={`thread-card-${thread.thread_id}`}
    >
      <div className="thread-home-head">
        {!sameName && (
          <button
            type="button"
            className="thread-project-chip"
            onClick={() => onOpenProject(thread.project)}
          >
            <span
              className="project-dot"
              style={project ? tintStyle(project.tint) : undefined}
              aria-hidden
            />
            <span>{projTitle}</span>
          </button>
        )}
        <ThreadMeta thread={thread} when={when} />
      </div>

      <button
        type="button"
        className="thread-home-main"
        onClick={() => onOpenThread(thread)}
      >
        <h3 className="thread-home-title">{title}</h3>
        <ChevronRight size={16} className="thread-home-chevron" aria-hidden />
      </button>

      <ThreadReply thread={thread} elapsed={when} compact actions={actions} />
      <ThreadOutputs
        links={thread.links ?? []}
        cards={thread.status === "idle" ? thread.cards : []}
        onOpenCards={() => onOpenThread(thread)}
      />
    </article>
  );
}

export function HomeView({
  threads,
  surfaces,
  projects,
  apps,
  ready,
  isSessionActive,
  onOpenThread,
  onOpenProject,
  actions,
  onDismissSurface,
  onSelectStarterPrompt,
  onOpenMore,
}: {
  threads: ThreadSummary[];
  surfaces: A2UISurface[];
  projects: ProjectSummary[];
  apps: AppInfo[];
  ready: boolean;
  isSessionActive: boolean;
  onOpenThread: (thread: ThreadSummary) => void;
  onOpenProject: (project: string) => void;
  actions: ThreadActions;
  onDismissSurface: (surface: A2UISurface) => void;
  onSelectStarterPrompt: (prompt: string) => void;
  onOpenMore: () => void;
}) {
  const now = useNow();
  const { needsYou, running, recent } = groupHomeThreads(threads);
  const contextCards = surfaces.filter((s) => s.kind === "context_card");
  const hasThreads =
    needsYou.length > 0 || running.length > 0 || recent.length > 0;

  if (!hasThreads && contextCards.length === 0) {
    const toConnect = appToConnect(apps);
    const connectCard = toConnect && <ConnectCard app={toConnect} />;
    if (isSessionActive)
      return connectCard ? <div className="home-now">{connectCard}</div> : null;
    return (
      <StarterSuggestions
        suggestions={starterSuggestions(apps)}
        onSelect={onSelectStarterPrompt}
      >
        {connectCard}
      </StarterSuggestions>
    );
  }

  const projectFor = (id: string) => projects.find((p) => p.id === id);

  const renderCard = (thread: ThreadSummary) => (
    <ThreadSummaryCard
      key={thread.thread_id}
      thread={thread}
      project={projectFor(thread.project)}
      now={now}
      actions={actions}
      onOpenThread={onOpenThread}
      onOpenProject={onOpenProject}
    />
  );

  return (
    <div className="home-now">
      {contextCards.length > 0 && (
        <A2UISurfaceDeck
          surfaces={contextCards}
          ready={ready}
          onDismissSurface={onDismissSurface}
        />
      )}

      {needsYou.length > 0 && (
        <section className="home-group" aria-label="Needs you">
          <h2 className="home-group-label" data-tone="attention">
            Needs you
          </h2>
          <div className="home-cards">{needsYou.map(renderCard)}</div>
        </section>
      )}

      {running.length > 0 && (
        <section className="home-group" aria-label="Running">
          <h2 className="home-group-label" data-tone="running">
            Running
          </h2>
          <div className="home-cards">{running.map(renderCard)}</div>
        </section>
      )}

      {recent.length > 0 && (
        <section className="home-group" aria-label="Recent">
          <div className="home-group-head">
            <h2 className="home-group-label">Recent</h2>
            {projects.length > 0 && (
              <Button
                variant="quiet"
                size="sm"
                className="home-more"
                data-testid="home-more"
                onClick={onOpenMore}
              >
                All projects
              </Button>
            )}
          </div>
          <div className="home-cards">{recent.map(renderCard)}</div>
        </section>
      )}
    </div>
  );
}
