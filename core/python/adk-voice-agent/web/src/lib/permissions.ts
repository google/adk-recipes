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

export type Risk = "high" | "medium" | "low";

export interface PermissionAction {
  title: string;
  detail?: string;
  risk: Risk;
}

/** Plain words for each `config/policy.yaml` `mcp_gates.ask` glob that pauses a thread. */
const PERMISSION_ACTIONS: Record<string, PermissionAction> = {
  "gmail.send*": {
    title: "Send an email",
    detail: "Sends a message from your Gmail account.",
    risk: "high",
  },
  "google_calendar.create*": {
    title: "Create a calendar event",
    detail: "Adds an event to your Google Calendar and invites any attendees.",
    risk: "high",
  },
  "github.merge*": {
    title: "Merge a pull request",
    detail: "Merges the pull request into its target branch.",
    risk: "high",
  },
  "github.create_pull_request*": {
    title: "Open a pull request",
    detail: "Creates a pull request on GitHub.",
    risk: "medium",
  },
  "github.delete*": {
    title: "Delete on GitHub",
    detail: "Removes a branch, file, or resource on GitHub.",
    risk: "high",
  },
  "github.push*": {
    title: "Push files to GitHub",
    detail: "Commits and pushes changes directly to a repository branch.",
    risk: "high",
  },
  "github.create_or_update_file*": {
    title: "Write a file on GitHub",
    detail: "Creates or updates a file directly in a repository branch.",
    risk: "high",
  },
  "github.create_branch*": {
    title: "Create a branch on GitHub",
    detail: "Creates a new branch in a GitHub repository.",
    risk: "medium",
  },
  "github.add_issue_comment*": {
    title: "Post a comment on GitHub",
    detail: "Posts a comment on an issue or pull request.",
    risk: "medium",
  },
  "github.add_comment*": {
    title: "Post a comment on GitHub",
    detail: "Posts a comment on GitHub.",
    risk: "medium",
  },
  "github.pull_request_review*": {
    title: "Submit a pull request review",
    detail: "Posts a review on a pull request.",
    risk: "medium",
  },
  "github.create_pull_request_review*": {
    title: "Submit a pull request review",
    detail: "Posts a review on a pull request.",
    risk: "medium",
  },
  "github.submit_pending_pull_request_review*": {
    title: "Submit a pull request review",
    detail: "Submits a pending pull request review.",
    risk: "medium",
  },
  "github.add_pull_request_review_comment*": {
    title: "Comment on a pull request diff",
    detail: "Posts a review comment on a pull request.",
    risk: "medium",
  },
  "github.create_issue*": {
    title: "Create a GitHub issue",
    detail: "Opens a new issue in a repository.",
    risk: "medium",
  },
  "github.update_*": {
    title: "Update on GitHub",
    detail: "Updates an issue or pull request on GitHub.",
    risk: "medium",
  },
  "github.update_issue*": {
    title: "Update a GitHub issue",
    detail: "Updates an existing issue on GitHub.",
    risk: "medium",
  },
  "github.update_pull_request*": {
    title: "Update a pull request",
    detail: "Updates an existing pull request on GitHub.",
    risk: "medium",
  },
  "github.create_repository*": {
    title: "Create a GitHub repository",
    detail: "Creates a new repository on your GitHub account.",
    risk: "high",
  },
  "github.fork_repository*": {
    title: "Fork a GitHub repository",
    detail: "Forks a repository to your GitHub account.",
    risk: "medium",
  },
  "github.*": {
    title: "Change on GitHub",
    detail: "Runs a write action on GitHub.",
    risk: "high",
  },
};

export const RISK_LABELS: Record<Risk, string> = {
  high: "High risk",
  medium: "Medium risk",
  low: "Low risk",
};

/** The known wording for a gate glob, or a generic high-risk fallback. */
export function describePermission(
  actionClass: string | undefined,
): PermissionAction & { known: boolean } {
  const key = actionClass ?? "";
  const known = PERMISSION_ACTIONS[key];
  if (known) return { ...known, known: true };
  if (key.startsWith("github.update_")) {
    return { ...PERMISSION_ACTIONS["github.update_*"], known: true };
  }
  if (key.startsWith("github.pull_request_review")) {
    return {
      ...PERMISSION_ACTIONS["github.pull_request_review*"],
      known: true,
    };
  }
  return {
    title: "Approve this action",
    risk: "high",
    known: false,
  };
}

export function formatPermissionArguments(
  args: Record<string, unknown> | undefined | null,
): string | null {
  if (!args || Object.keys(args).length === 0) return null;
  return JSON.stringify(args, null, 2);
}

export const DECLINE_INSTRUCTION =
  "The user declined this on screen without giving a reason. " +
  "Don't do it; continue without it.";
