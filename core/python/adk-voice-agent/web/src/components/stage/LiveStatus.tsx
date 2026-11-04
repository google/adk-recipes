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

import { Pause } from "lucide-react";
import type { LiveAudioState } from "../../lib/live-audio-client";

/** What the status line says while each voice tool (`app/agent.py`) runs. */
const TOOL_ACTIVITY: Record<string, string> = {
  start_thread: "Starting a thread",
  message_thread: "Sending to thread",
  approve: "Approving",
  cancel_thread: "Stopping the thread",
  read_thread: "Checking thread",
  list_threads: "Checking your threads",
  list_projects: "Checking your projects",
  name_project: "Naming project",
  publish_thread: "Publishing branch",
  share_context: "Sharing context",
  navigate: "Opening on screen",
  look: "Looking that up",
};

function activityLabel(toolName: string): string {
  return TOOL_ACTIVITY[toolName] ?? "Working on it";
}

function ActivityDots() {
  return (
    <span className="activity-dots" aria-hidden>
      <span />
      <span />
      <span />
    </span>
  );
}

function StatusText({ live }: { live: LiveAudioState }) {
  if (live.activeToolCall) {
    return (
      <span className="caption-status">
        <ActivityDots />
        {activityLabel(live.activeToolCall)}
      </span>
    );
  }
  switch (live.status) {
    case "connecting":
      return (
        <span className="caption-status">
          <ActivityDots />
          Connecting
        </span>
      );
    case "held":
      return (
        <span className="caption-status">
          <Pause size={12} />
          On hold
        </span>
      );
    case "listening":
      return <span className="caption-status">Listening</span>;
    case "mic_blocked":
      return (
        <span className="caption-status">
          Microphone blocked: allow it in your browser
        </span>
      );
    case "error":
      return <span className="caption-status">{live.substatus}</span>;
    case "speaking":
      return null;
    default:
      return <span className="caption-status">{live.substatus}</span>;
  }
}

/**
 * The bottom of the stage: one quiet line on what the session is doing
 * (connecting, listening, on hold, a tool at work). What the voice says is heard,
 * not captioned; its summaries show with each thread.
 */
export function LiveStatus({ live }: { live: LiveAudioState }) {
  return (
    <div
      className="stage-caption-zone"
      id="live-activity-pill"
      aria-live="polite"
    >
      <StatusText live={live} />
    </div>
  );
}
