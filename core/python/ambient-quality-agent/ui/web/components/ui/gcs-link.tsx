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
import { ExternalLink } from "lucide-react";

/**
 * `gs://bucket/a/b.md` as a Cloud console object URL, or `null`.
 *
 * Uses the public `console.cloud.google.com` host because it works for every
 * Google Cloud account.
 */
export function gcsConsoleUrl(uri: string | null | undefined): string | null {
  if (!uri?.startsWith("gs://")) return null;
  const path = uri.slice("gs://".length);
  if (!path.includes("/")) return null;
  return `https://console.cloud.google.com/storage/browser/_details/${path}`;
}

/** Opens the backing object in the Cloud console. Renders nothing without a URI. */
export function GcsLink({ uri }: { uri: string | null | undefined }) {
  const href = gcsConsoleUrl(uri);
  if (!href) return null;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={uri ?? undefined}
      className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
    >
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
      GCS
    </a>
  );
}
