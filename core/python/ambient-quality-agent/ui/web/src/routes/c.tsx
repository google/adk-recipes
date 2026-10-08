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

import { createFileRoute } from "@tanstack/react-router";
import { ChatRoute } from "@/components/chat/chat-route";

export const Route = createFileRoute("/c")({
  validateSearch: (
    search: Record<string, unknown>,
  ): { id?: string; q?: string } => ({
    id: typeof search.id === "string" ? search.id : undefined,
    // A question typed on the dashboard, sent once the chat client is up.
    q: typeof search.q === "string" ? search.q : undefined,
  }),
  component: ChatRoute,
});
