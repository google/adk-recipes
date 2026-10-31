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
  CircleUserRound,
  Folder,
  GitBranch,
  House,
  MessageSquare,
  Settings,
  X,
} from "lucide-react";
import { useRef, useState } from "react";
import {
  type ProjectSummary,
  type ThreadSummary,
  useOrchestratorConfig,
} from "../../lib/api";
import { timeAgo } from "../../lib/format";
import {
  drawerSections,
  recentRows,
  searchProjects,
  tintStyle,
} from "../../lib/projects";
import { Section } from "../common/primitives";
import {
  Button,
  EmptyState,
  IconButton,
  InlineConfirm,
  Row,
  TextInput,
} from "../common/ui";
import { useModalFocus } from "../common/useModalFocus";

const KIND_LABELS: Record<ProjectSummary["kind"], string> = {
  project: "Project",
  code: "Code",
  scratch: "One-off ask",
};

const KIND_ICONS: Record<ProjectSummary["kind"], typeof Folder> = {
  project: Folder,
  code: GitBranch,
  scratch: MessageSquare,
};

function ProjectIcon({ project }: { project: ProjectSummary }) {
  const Icon = KIND_ICONS[project.kind];
  return (
    <Icon
      size={17}
      aria-hidden
      className="project-icon"
      data-kind={project.kind}
      style={tintStyle(project.tint)}
    />
  );
}

const SWIPE_CLOSE_PX = 60;

function NeedsYou({ count }: { count: number }) {
  return (
    <span className="project-activity" data-tone="attention">
      {count} need{count === 1 ? "s" : ""} you
    </span>
  );
}

function ProjectActivity({ project }: { project: ProjectSummary }) {
  if (project.needs_you > 0) return <NeedsYou count={project.needs_you} />;
  if (project.running > 0)
    return (
      <span className="project-activity" data-tone="running">
        {project.running} running
      </span>
    );
  return (
    <span className="project-activity">
      {timeAgo(project.last_activity_at)}
    </span>
  );
}

function ProjectRows({
  projects,
  onOpen,
}: {
  projects: ProjectSummary[];
  onOpen: (project: string) => void;
}) {
  return (
    <ul className="project-list">
      {projects.map((p) => (
        <li key={p.id}>
          <Row
            icon={<ProjectIcon project={p} />}
            title={p.title}
            subtitle={p.status_line || KIND_LABELS[p.kind]}
            trailing={<ProjectActivity project={p} />}
            onClick={() => onOpen(p.id)}
          />
        </li>
      ))}
    </ul>
  );
}

export function NavDrawer({
  projects,
  loading,
  threads,
  onOpenHome,
  onOpenProject,
  onOpenSettings,
  onDeleteProjects,
  onClose,
}: {
  projects: ProjectSummary[];
  loading: boolean;
  threads: ThreadSummary[];
  onOpenHome: () => void;
  onOpenProject: (project: string) => void;
  onOpenSettings: () => void;
  onDeleteProjects: (projects: string[]) => void;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const swipe = useRef<{ x: number; y: number }>();
  const [query, setQuery] = useState("");
  const [recentOpen, setRecentOpen] = useState(false);
  useModalFocus(panel, onClose);
  const { data: config } = useOrchestratorConfig();

  const homeNeedsYou = threads.filter((t) => t.is_gate).length;
  const { needYou, places, recent } = drawerSections(projects);
  const { shown: recentShown, hidden: recentHidden } = recentRows(
    recent,
    recentOpen,
  );
  const found = query.trim() ? searchProjects(projects, query) : null;

  return (
    <div className="drawer-backdrop">
      {/* Tapping outside the panel closes the drawer; Esc is the keyboard way out. */}
      <button
        type="button"
        className="sheet-scrim"
        tabIndex={-1}
        aria-hidden
        onClick={onClose}
      />
      <div
        ref={panel}
        className="drawer-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Menu"
        tabIndex={-1}
        data-testid="nav-drawer"
        onTouchStart={(e) => {
          const t = e.touches[0];
          swipe.current = { x: t.clientX, y: t.clientY };
        }}
        onTouchEnd={(e) => {
          const start = swipe.current;
          const t = e.changedTouches[0];
          swipe.current = undefined;
          if (!start) return;
          const dx = t.clientX - start.x;
          if (
            dx < -SWIPE_CLOSE_PX &&
            Math.abs(dx) > Math.abs(t.clientY - start.y)
          )
            onClose();
        }}
      >
        {/* The same header as every sheet: a title and a close button. */}
        <div className="sheet-header">
          <span className="sheet-title">ADK Voice Agent</span>
          <button
            type="button"
            className="sheet-close-btn"
            onClick={onClose}
            aria-label="Close menu"
          >
            <X size={16} />
          </button>
        </div>
        <div className="drawer-body">
          <TextInput
            type="search"
            placeholder="Find a project"
            aria-label="Find a project"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />

          {found ? (
            found.length > 0 ? (
              <ProjectRows projects={found} onOpen={onOpenProject} />
            ) : (
              <EmptyState title="No matching projects">
                Try another word, or ask ADK Voice Agent.
              </EmptyState>
            )
          ) : (
            <>
              <Row
                testId="nav-drawer-home"
                icon={<House size={16} />}
                title="Home"
                trailing={
                  homeNeedsYou > 0 ? (
                    <NeedsYou count={homeNeedsYou} />
                  ) : undefined
                }
                onClick={onOpenHome}
              />

              {needYou.length > 0 && (
                <Section label="Needs you">
                  <ProjectRows projects={needYou} onOpen={onOpenProject} />
                </Section>
              )}

              <Section label="Projects">
                {places.length > 0 && (
                  <ProjectRows projects={places} onOpen={onOpenProject} />
                )}
                {projects.length === 0 && !loading && (
                  <EmptyState title="No projects yet">
                    Ask ADK Voice Agent for something, and it gets a project
                    here.
                  </EmptyState>
                )}
              </Section>

              {recent.length > 0 && (
                <Section
                  label="Recent"
                  aside={
                    <InlineConfirm
                      question={`Clear ${recent.length}?`}
                      confirmLabel="Clear"
                      onConfirm={() =>
                        onDeleteProjects(recent.map((p) => p.id))
                      }
                      trigger={(ask) => (
                        <Button size="sm" variant="quiet" onClick={ask}>
                          Clear
                        </Button>
                      )}
                    />
                  }
                >
                  <ProjectRows projects={recentShown} onOpen={onOpenProject} />
                  {(recentHidden > 0 || recentOpen) && (
                    <Button
                      size="sm"
                      variant="quiet"
                      className="drawer-more"
                      aria-expanded={recentOpen}
                      onClick={() => setRecentOpen((open) => !open)}
                    >
                      {recentOpen ? "Show fewer" : `Show all ${recent.length}`}
                    </Button>
                  )}
                </Section>
              )}
            </>
          )}
        </div>

        {/* The account as Settings shows it: a row, with Settings one tap away. */}
        <footer className="drawer-footer">
          <Row
            icon={<CircleUserRound size={17} />}
            title={config?.account.user_id ?? ""}
            trailing={
              <IconButton
                id="open-settings-btn"
                label="Settings"
                onClick={onOpenSettings}
              >
                <Settings size={17} />
              </IconButton>
            }
          />
        </footer>
      </div>
    </div>
  );
}
