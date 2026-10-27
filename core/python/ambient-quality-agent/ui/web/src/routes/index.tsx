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
import { HomeView } from "@/components/home/home-view";
import { PageShell } from "@/components/nav/page-shell";

export const Route = createFileRoute("/")({
  // The homepage answers "is AQuA working?" before it asks you anything.
  // Chat is a click away and the ask bar hands straight to it.
  component: function Home() {
    return (
      <PageShell width="wide">
        <HomeView />
      </PageShell>
    );
  },
});
