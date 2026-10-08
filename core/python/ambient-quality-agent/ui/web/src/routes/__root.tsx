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

import { createRootRoute, HeadContent, Outlet } from "@tanstack/react-router";
import { QueryProvider } from "@/app/providers";
import { TabBar } from "@/components/nav/tab-bar";
import { TourOverlay } from "@/components/tour/tour-overlay";
import { usePrefetchAqua } from "@/lib/aqua-prefetch";
import { TooltipProvider } from "@/components/ui/tooltip";
import "../fonts.css";
import "@/app/globals.css";

function RootComponent() {
  return (
    <QueryProvider>
      <PrefetchOnBoot />
      <TooltipProvider delayDuration={300}>
        <HeadContent />
        <div className="flex h-dvh min-h-0 flex-col">
          <TabBar />
          <div className="min-h-0 flex-1">
            <Outlet />
          </div>
        </div>
        <TourOverlay />
      </TooltipProvider>
    </QueryProvider>
  );
}

/** Inside QueryProvider so it can reach the client; renders nothing. */
function PrefetchOnBoot() {
  usePrefetchAqua();
  return null;
}

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { title: "AQuA" },
      {
        name: "description",
        content: "A self-improving coding agent built on Google's ADK.",
      },
    ],
  }),
  component: RootComponent,
});
