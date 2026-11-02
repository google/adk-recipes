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

import { AlertTriangle, ShieldAlert, Square } from "lucide-react";
import { useState } from "react";
import type { ThreadPermission, ThreadSummary } from "../../../lib/api";
import {
  describePermission,
  formatPermissionArguments,
  RISK_LABELS,
} from "../../../lib/permissions";
import { LiveLine } from "../../common/primitives";
import { Button } from "../../common/ui";

/** What the user can do to a thread by tap; every view wires the same three. */
export interface ThreadActions {
  onApprove: (thread: ThreadSummary) => void;
  onDecline: (thread: ThreadSummary) => void;
  onCancel: (thread: ThreadSummary) => void;
}

/** A gated call waiting on the user: what it wants to do, Allow, and Decline. */
export function ApprovalBox({
  permission,
  onApprove,
  onDecline,
}: {
  permission: ThreadPermission;
  onApprove: () => void;
  onDecline: () => void;
}) {
  const [showFull, setShowFull] = useState(false);
  const desc = describePermission(permission.action_class);
  const title = permission.summary || desc.title;
  const fullArgs = formatPermissionArguments(permission.arguments);

  return (
    <section
      className="thread-approval-box"
      aria-label="Approval requested"
      data-testid="thread-approval-box"
    >
      <div className="thread-approval-head">
        <ShieldAlert size={16} aria-hidden />
        <span className="thread-approval-title">{title}</span>
        <span className="thread-approval-risk">{RISK_LABELS[desc.risk]}</span>
      </div>
      {desc.detail && <p className="thread-approval-detail">{desc.detail}</p>}
      {permission.command && (
        <div className="thread-command-well">
          {permission.kind !== "tool" && (
            <span className="thread-command-prompt" aria-hidden>
              $
            </span>
          )}
          <code>{permission.command}</code>
        </div>
      )}
      {fullArgs && (
        <>
          <Button
            size="sm"
            variant="quiet"
            className="thread-request-toggle"
            aria-expanded={showFull}
            onClick={() => setShowFull((open) => !open)}
          >
            {showFull ? "Hide full request" : "Show full request"}
          </Button>
          {showFull && (
            <pre
              className="thread-command-well thread-arguments-well"
              data-testid="thread-approval-arguments"
            >
              <code>{fullArgs}</code>
            </pre>
          )}
        </>
      )}
      <div className="thread-approval-actions">
        <Button size="sm" variant="primary" onClick={onApprove}>
          Allow
        </Button>
        <Button size="sm" variant="secondary" onClick={onDecline}>
          Decline
        </Button>
      </div>
    </section>
  );
}

/** A running thread: its latest line, how long it has run, and Stop. */
export function RunningBar({
  latestUpdate,
  elapsed,
  onCancel,
}: {
  latestUpdate: string;
  elapsed: string;
  onCancel: () => void;
}) {
  return (
    <div
      className="thread-running-bar"
      role="status"
      data-testid="thread-running-bar"
    >
      <span className="thread-running-dot" aria-hidden />
      <span className="thread-running-text">
        <LiveLine text={latestUpdate || "Working…"} />
      </span>
      <span className="thread-running-elapsed">{elapsed}</span>
      <Button
        size="sm"
        variant="quiet"
        data-tone="danger"
        icon={<Square size={12} aria-hidden />}
        iconFirst
        onClick={onCancel}
      >
        Stop
      </Button>
    </div>
  );
}

export function TurnError({ error }: { error?: string | null }) {
  if (!error) return null;
  return (
    <p className="chat-turn-error" role="alert">
      <AlertTriangle size={14} aria-hidden />
      <span>{error}</span>
    </p>
  );
}

/**
 * What the thread says back, the same on Home, in a project, and in the thread:
 * the approval it waits on, its live line with Stop, its error, or the summary the
 * voice spoke. `compact` (Home) clamps the summary to two lines.
 */
export function ThreadReply({
  thread,
  elapsed,
  compact,
  actions,
}: {
  thread: ThreadSummary;
  elapsed: string;
  compact?: boolean;
  actions: ThreadActions;
}) {
  if (thread.status === "awaiting_approval" && thread.permission) {
    return (
      <ApprovalBox
        permission={thread.permission}
        onApprove={() => actions.onApprove(thread)}
        onDecline={() => actions.onDecline(thread)}
      />
    );
  }
  if (thread.status === "running") {
    return (
      <RunningBar
        latestUpdate={thread.latest_update}
        elapsed={elapsed}
        onCancel={() => actions.onCancel(thread)}
      />
    );
  }
  const summary = (
    <p
      className="thread-reply-summary"
      data-compact={compact ? true : undefined}
    >
      {thread.summary || thread.goal}
    </p>
  );
  if (thread.error) {
    return (
      <>
        <TurnError error={thread.error} />
        {/* It failed after it answered: the answer still stands. */}
        {thread.result_text.trim() && summary}
      </>
    );
  }
  if (thread.outcome === "cancelled") {
    return <p className="chat-turn-muted">Stopped.</p>;
  }
  return summary;
}
