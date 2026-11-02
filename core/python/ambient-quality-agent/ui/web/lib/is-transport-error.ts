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

// Detect "the backend is unreachable" failures so the UI can swap inline
// stream-error trailers for a top-level "session lost" banner. `fetch`
// failures surface as `TypeError`; the regex covers the platform-specific
// message strings we've seen (Chromium, Firefox, Safari, Node).
const TRANSPORT_MESSAGE_RE =
  /Failed to fetch|NetworkError|network error|ECONNREFUSED/i;

export function isTransportError(err: unknown): boolean {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    return true;
  }
  if (err instanceof TypeError) return true;
  const message = err instanceof Error ? err.message : String(err ?? "");
  return TRANSPORT_MESSAGE_RE.test(message);
}
