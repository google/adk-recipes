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

import { useSearch } from "@tanstack/react-router";
import { ChatShell } from "./chat-shell";

// Shared by both the `/` and `/c` routes so each reads an optional `?id=` the
// same way. The first send locks the contextId by adding `?id=…` on the current
// route (`/` stays `/`, becoming `/?id=…`) — a search-only change that does NOT
// remount, so the in-flight stream and pending bubble survive. (A pathname
// change `/`→`/c` WOULD remount under TanStack Router and abort the stream.)
export function ChatRoute() {
  const search = useSearch({ strict: false }) as { id?: string; q?: string };
  const id = typeof search.id === "string" ? search.id : undefined;
  const q = typeof search.q === "string" ? search.q : undefined;
  return <ChatShell contextId={id} initialMessage={q} />;
}
