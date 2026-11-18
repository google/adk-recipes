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

import { Mic, Pause, Play, X } from "lucide-react";
import { useEffect, useReducer, useRef, useState } from "react";
import { AppConnectProvider } from "./components/common/AppConnect";
import { ConnectionsSheet } from "./components/sheets/connections/ConnectionsSheet";
import { GeminiAuroraCanvas } from "./components/stage/GeminiAuroraCanvas";
import { HomeView } from "./components/stage/HomeView";
import { LiveStatus } from "./components/stage/LiveStatus";
import { NavDrawer } from "./components/stage/NavDrawer";
import { StageHeader } from "./components/stage/StageHeader";
import { ThreadView } from "./components/stage/ThreadView";
import type { ThreadActions } from "./components/stage/thread/ThreadReply";
import {
  type A2UISurface,
  type OrchestratorState,
  postFocus,
  type ThreadSummary,
  useOrchestratorActions,
  useOrchestratorConfig,
  useOrchestratorState,
  useProjects,
  useRefreshProjects,
  useRefreshState,
  useRefreshThread,
} from "./lib/api";
import { isConnected, useGeminiLiveAudio } from "./lib/live-audio-client";
import {
  appConnectedTurn,
  focusPayload,
  HOME,
  isNavigationTool,
  navigate,
  navigateApp,
  navigateView,
} from "./lib/navigation";
import { DECLINE_INSTRUCTION } from "./lib/permissions";
import { folderTitle, needsYouElsewhere, tintStyle } from "./lib/projects";
import { threadTitle } from "./lib/threads";

/** Shown until the first `/state` response arrives (or while the backend is unreachable). */
const FALLBACK_STATE: OrchestratorState = {
  threads: [],
  a2ui_surfaces: [],
};

/** What covers the stage; one at a time. */
type Overlay = "drawer" | "settings";

/** Runs `effect` when `value` changes after the first render. */
function useOnChange(value: string, effect: () => void) {
  const last = useRef(value);
  useEffect(() => {
    if (last.current === value) return;
    last.current = value;
    effect();
  });
}

export function App() {
  const { data: serverState, isError: isHealthError } = useOrchestratorState();
  const projectsQuery = useProjects();
  const projects = projectsQuery.data ?? [];
  const apps = useOrchestratorConfig().data?.apps ?? [];
  const [actionError, setActionError] = useState<string | null>(null);
  const actions = useOrchestratorActions(setActionError);
  const refreshState = useRefreshState();
  const refreshProjects = useRefreshProjects();
  const refreshThread = useRefreshThread();

  useEffect(() => {
    if (!actionError) return;
    const timer = window.setTimeout(() => setActionError(null), 5000);
    return () => window.clearTimeout(timer);
  }, [actionError]);

  const [view, go] = useReducer(navigate, HOME);
  const [overlay, setOverlay] = useState<Overlay>();
  // The app Settings opens on, when the voice asked for one.
  const [settingsApp, setSettingsApp] = useState<string>();

  const state = serverState || FALLBACK_STATE;
  const threads = state.threads;
  const stageSurfaces = state.a2ui_surfaces.filter((s) => s.slot !== "dock");
  const threadsRef = useRef(threads);
  threadsRef.current = threads;

  // Sync OPEN ON SCREEN focus to the server whenever the view changes.
  useEffect(() => {
    void postFocus(focusPayload(view)).catch(() => {});
  }, [view]);

  /** Follow the voice's `navigate` tool call or response. */
  const followVoice = (args: Record<string, unknown>) => {
    const app = navigateApp(args);
    if (app) {
      setSettingsApp(app);
      setOverlay("settings");
      return;
    }
    const next = navigateView(args, threadsRef.current);
    if (!next) return;
    setOverlay((open) => (open === "drawer" ? undefined : open));
    go(next);
  };

  const liveAudio = useGeminiLiveAudio({
    onToolCall: ({ name, args }) => {
      if (isNavigationTool(name)) followVoice(args);
    },
    onToolResponse: ({ name, response }) => {
      void refreshState();
      if (isNavigationTool(name) && response.ok) followVoice(response);
    },
    onTurnComplete: () => void refreshThread(),
  });
  const connected = isConnected(liveAudio.status);

  // A thread started, moved, or ended: projects and the open view catch up.
  useOnChange(
    threads.map((t) => `${t.thread_id}:${t.status}:${t.last_seq}`).join(","),
    () => void refreshProjects(),
  );

  /** Tell the voice about an on-screen action so it doesn't repeat or contradict it. */
  const tellVoice = (text: string) => {
    if (connected) liveAudio.sendTextTurn(text);
  };

  const openProject = (project: string) => {
    setOverlay(undefined);
    go({ view: "project", project });
  };

  const openThread = (thread: ThreadSummary) => {
    setOverlay(undefined);
    go({
      view: "thread",
      threadId: thread.thread_id,
      project: thread.project,
      title: threadTitle(thread),
    });
  };

  const goHome = () => {
    setOverlay(undefined);
    go(HOME);
  };

  const startLive = () => {
    void liveAudio.startSession();
  };

  const handleApproveThread = (
    threadId: string,
    permissionKey?: string,
    title?: string,
  ) => {
    actions.approveThread.mutate(
      { threadId, permissionKey },
      {
        onSuccess: () =>
          tellVoice(
            `I approved ${title || threadId} on screen. It is already continuing; acknowledge briefly.`,
          ),
      },
    );
  };

  /** Allow, Decline, and Stop by tap: the same on Home, in a project, and in a thread. */
  const threadActions: ThreadActions = {
    onApprove: (t) =>
      handleApproveThread(t.thread_id, t.permission?.key, threadTitle(t)),
    onDecline: (t) =>
      actions.messageThread.mutate(
        { threadId: t.thread_id, instruction: DECLINE_INSTRUCTION },
        {
          onSuccess: () =>
            tellVoice(
              `I declined ${threadTitle(t)}'s request on screen. It is already continuing without it; acknowledge briefly.`,
            ),
        },
      ),
    onCancel: (t) => actions.cancelThread.mutate(t.thread_id),
  };
  const deleteThread = (threadId: string) =>
    actions.deleteThread.mutate(threadId);

  const handleDismissSurface = (surface: A2UISurface) => {
    actions.dismissSurface.mutate(surface.surfaceId);
  };

  const activeThread =
    view.view === "thread"
      ? threads.find((t) => t.thread_id === view.threadId)
      : undefined;
  const activeProjectId =
    view.view === "project"
      ? view.project
      : view.view === "thread"
        ? (view.project ?? activeThread?.project)
        : undefined;
  const project = activeProjectId
    ? projects.find((p) => p.id === activeProjectId)
    : undefined;
  const projectTitle = activeProjectId
    ? (project?.title ?? folderTitle(activeProjectId))
    : "";

  const headerThread =
    view.view === "thread"
      ? {
          title: activeThread
            ? threadTitle(activeThread)
            : (view.title ?? view.threadId),
          statusLine: projectTitle,
          onHome: activeProjectId ? () => openProject(activeProjectId) : goHome,
          onDelete: () => {
            deleteThread(view.threadId);
            if (activeProjectId) openProject(activeProjectId);
            else goHome();
          },
        }
      : view.view === "project"
        ? {
            title: projectTitle,
            statusLine: project?.status_line ?? "",
            onHome: goHome,
            // A one-off has no menu: the voice keeps it as a project when it's ongoing.
            project:
              project && project.kind !== "scratch"
                ? {
                    name: project.title,
                    hideLabel: project.kind === "code" ? "Hide" : "Archive",
                    onRename: (name: string) =>
                      actions.updateProject.mutate({
                        project: project.id,
                        update: { name },
                      }),
                    onHide: () => {
                      actions.updateProject.mutate({
                        project: project.id,
                        update: { hidden: true },
                      });
                      goHome();
                    },
                  }
                : undefined,
          }
        : undefined;

  const openOverlay = (next: Overlay) => {
    setSettingsApp(undefined);
    setOverlay(next);
  };

  /** An app finished connecting: a live voice hears it, and carries on with what needed it. */
  const appConnected = (name: string) =>
    tellVoice(
      appConnectedTurn(apps.find((a) => a.name === name)?.display_name ?? name),
    );
  const openDrawer = () => openOverlay("drawer");

  return (
    <AppConnectProvider onConnected={appConnected}>
      <main className="orchestrator-viewport">
        <div className="mobile-device-shell">
          <h1 className="visually-hidden">ADK Voice Agent</h1>

          <section
            className="gemini-stage-container"
            aria-label="ADK Voice Agent Stage"
          >
            <GeminiAuroraCanvas
              energyRef={liveAudio.audioEnergyRef}
              status={liveAudio.status}
            />
            {/* Inside a project or thread the aurora takes the project's tint; home keeps the default. */}
            <div
              className="stage-tint"
              style={project ? tintStyle(project.tint) : undefined}
              data-on={project ? true : undefined}
              aria-hidden
            />

            <StageHeader
              thread={headerThread}
              attention={needsYouElsewhere(
                threads,
                view.view === "thread"
                  ? { threadId: view.threadId, project: activeProjectId }
                  : view.view === "project"
                    ? { project: view.project }
                    : undefined,
              )}
              onOpenMenu={openDrawer}
            />

            {isHealthError ? (
              <p className="stage-banner" role="status">
                Can't connect right now. Retrying…
              </p>
            ) : actionError ? (
              <p className="stage-banner" role="alert">
                {actionError}
              </p>
            ) : null}

            <div className="stage-middle-deck">
              {view.view === "project" || view.view === "thread" ? (
                <ThreadView
                  key={
                    view.view === "thread"
                      ? `thread:${view.threadId}`
                      : `project:${view.project}`
                  }
                  project={activeProjectId}
                  threadId={view.view === "thread" ? view.threadId : undefined}
                  threads={threads}
                  actions={threadActions}
                  onSelectThread={openThread}
                />
              ) : (
                <HomeView
                  threads={threads}
                  surfaces={stageSurfaces}
                  projects={projects}
                  apps={apps}
                  ready={serverState !== undefined}
                  isSessionActive={
                    liveAudio.status !== "idle" && liveAudio.status !== "error"
                  }
                  onOpenThread={openThread}
                  onOpenProject={openProject}
                  actions={threadActions}
                  onDismissSurface={handleDismissSurface}
                  onSelectStarterPrompt={(prompt) =>
                    void liveAudio.startSession(prompt)
                  }
                  onOpenMore={openDrawer}
                />
              )}
            </div>

            <LiveStatus live={liveAudio} />

            {overlay === "drawer" && (
              <NavDrawer
                projects={projects}
                loading={projectsQuery.isPending}
                threads={threads}
                onOpenHome={goHome}
                onOpenProject={(id) => openProject(id)}
                onOpenSettings={() => openOverlay("settings")}
                onDeleteProjects={(ids) => actions.deleteProjects.mutate(ids)}
                onClose={() => setOverlay(undefined)}
              />
            )}

            {overlay === "settings" && (
              <ConnectionsSheet
                key={settingsApp ?? "root"}
                app={settingsApp}
                onClose={() => setOverlay(undefined)}
              />
            )}
          </section>

          <nav className="gemini-bottom-dock" aria-label="Voice Controls">
            <div className="dock-controls">
              {liveAudio.status === "idle" || liveAudio.status === "error" ? (
                <div className="dock-control-item">
                  <button
                    type="button"
                    id="start-live-session-btn"
                    className="dock-circle-btn start-live-btn"
                    onClick={startLive}
                    aria-label={
                      activeProjectId
                        ? `Talk about ${projectTitle}`
                        : "Start Gemini Live Session"
                    }
                  >
                    <Mic size={23} />
                  </button>
                  <span className="dock-label">Live</span>
                </div>
              ) : (
                <>
                  <div className="dock-control-item">
                    <button
                      type="button"
                      id="hold-voice-btn"
                      className={`dock-circle-btn ${liveAudio.status === "held" ? "hold-active" : ""}`}
                      onClick={() => liveAudio.toggleHold()}
                      aria-label={
                        liveAudio.status === "held"
                          ? "Resume Voice"
                          : "Hold Voice"
                      }
                    >
                      {liveAudio.status === "held" ? (
                        <Play size={22} />
                      ) : (
                        <Pause size={22} />
                      )}
                    </button>
                    <span className="dock-label">
                      {liveAudio.status === "held" ? "Resume" : "Hold"}
                    </span>
                  </div>

                  <div className="dock-control-item">
                    <button
                      type="button"
                      id="end-voice-btn"
                      className="dock-circle-btn end-btn"
                      onClick={() => liveAudio.endSession()}
                      aria-label="End Gemini Live Session"
                    >
                      <X size={24} />
                    </button>
                    <span className="dock-label">End</span>
                  </div>
                </>
              )}
            </div>
          </nav>
        </div>
      </main>
    </AppConnectProvider>
  );
}
