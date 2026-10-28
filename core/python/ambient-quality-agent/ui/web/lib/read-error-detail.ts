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

// FastAPI's HTTPException serializes as {"detail": "..."} JSON. Calling
// r.text() directly leaks the raw envelope into the UI; this helper reads
// the body once and extracts the message field when present.
export async function readErrorDetail(r: Response): Promise<string> {
  const body = await r.text();
  if (!body) return `${r.status}`;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (parsed.detail != null) return JSON.stringify(parsed.detail);
  } catch {
    // not JSON — fall through to raw body
  }
  return body;
}
