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

import type { ConversationFile } from "@/lib/conversation";
import { resolveFileIcon } from "@/lib/file-icon";
import { mono, textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/**
 * Renders one chip per file attached to a message.
 *
 * Chips are not links because telemetry URIs (such as `gs://` URIs or server
 * paths) cannot be opened directly in a browser. The URI is shown on hover and
 * in the step ledger's Raw tab.
 *
 * @param props.files - Files attached to the message.
 * @returns List of file chips.
 */
export function FileChips({ files }: { files: ConversationFile[] }) {
  return (
    <ul aria-label="Attached files" className="flex flex-wrap gap-1.5">
      {files.map((file, i) => {
        const Icon = resolveFileIcon(file.mimeType);
        return (
          <li
            // biome-ignore lint/suspicious/noArrayIndexKey: the same file can be attached twice, so neither the URI nor the type is unique.
            key={i}
            title={file.uri ?? undefined}
            className={cn(
              textStyle.label,
              mono,
              "inline-flex max-w-full items-center gap-1 rounded border border-border bg-muted px-1.5 py-0.5",
            )}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 truncate">{file.mimeType || "File"}</span>
          </li>
        );
      })}
    </ul>
  );
}
