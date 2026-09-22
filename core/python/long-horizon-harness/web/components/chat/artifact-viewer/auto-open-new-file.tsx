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

import { useEffect, useRef, useState } from "react";
import {
  isAgentWorkingFile,
  listWorkspaceFiles,
  type WorkspaceFiles,
} from "@/lib/workspace-files";
import { useBusy } from "../busy-context";
import { useViewer } from "./viewer-context";

/**
 * Opens the files a turn produced, so asking for a report doesn't also require
 * hunting for it.
 *
 * One rule: at the end of a turn, open everything that appeared. Tabs are keyed
 * by path, so a file that is already open is refreshed rather than duplicated,
 * and the newest ends up in front. Detection is a listing diff rather than a
 * scan for `write_file` calls, because a rename is `mv` in the terminal and
 * produces no write tool call at all.
 *
 * One exception: agent working material (`plan.md`, `scripts/`, skills,
 * dot-files) never opens. It is usually written last, so without this it would
 * sit in front of the thing that was actually asked for.
 *
 * Renders nothing; it lives inside ViewerProvider because ChatShell creates
 * that provider and so cannot call into it.
 */
export function AutoOpenNewFile() {
  const busy = useBusy();
  const { openArtifact } = useViewer();
  const [pending, setPending] = useState<string[]>([]);

  // The listing as of the last settled moment, diffed at the next turn end.
  const knownRef = useRef<WorkspaceFiles | null>(null);
  useEffect(() => {
    let cancelled = false;
    void listWorkspaceFiles()
      .then((m) => {
        if (!cancelled && knownRef.current === null) knownRef.current = m;
      })
      // A transient tree failure must not become an unhandled rejection; the
      // next turn end tries again.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const wasBusy = useRef(busy);
  useEffect(() => {
    const started = !wasBusy.current && busy;
    const ended = wasBusy.current && !busy;
    wasBusy.current = busy;

    // Candidates belong to the turn that produced them.
    if (started) setPending([]);
    if (!ended) return;

    let cancelled = false;
    void listWorkspaceFiles()
      .then((now) => {
        if (cancelled) return;
        const before = knownRef.current;
        knownRef.current = now;
        if (!before) return; // First listing still in flight; nothing to diff.

        const candidates = [...now.entries()]
          .filter(([path]) => !isAgentWorkingFile(path))
          .filter(([path]) => !before.has(path))
          // Oldest first: each open activates its tab, so the newest lands in
          // front without needing to reorder afterwards.
          .sort((a, b) => a[1] - b[1])
          .map(([path]) => path);

        if (candidates.length > 0) setPending(candidates);
      })
      // Leaving the baseline untouched is right: the next turn diffs against
      // the last listing that actually arrived.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [busy]);

  useEffect(() => {
    if (pending.length === 0) return;
    setPending([]);
    for (const path of pending) {
      openArtifact({
        name: path.split("/").pop() || path,
        // pickRenderer falls back to the extension, which is all a path tells
        // us.
        mimeType: "",
        path,
        url: `/lha/workspace/download?path=${encodeURIComponent(path)}&inline=1`,
      });
    }
  }, [pending, openArtifact]);

  return null;
}
