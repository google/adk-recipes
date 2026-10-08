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

import type { QueryClient } from "@tanstack/react-query";
import { insightsQuery, runsQuery, type Run } from "@/lib/aqua-api";
import { startedAt } from "@/lib/runstate";

/** One stop of the guided tour: a spotlight on part of a page and what to say
 *  about it. */
export interface TourStep {
  /** The `data-tour` attribute of the element the spotlight falls on. */
  target: string;
  title: string;
  body: string;
  /** The page the step is on, when it is not whatever page is open. A page
   *  that needs an id out of the data is a function, which resolves null when
   *  the deployment has none to show and the tour skips the step. */
  page?: string | ((client: QueryClient) => Promise<string | null>);
}

/** The insight the Home strip ranks first. */
async function topInsightPage(client: QueryClient): Promise<string | null> {
  const page = await client.fetchQuery(
    insightsQuery({ orderBy: "impact", pageSize: 1 }),
  );
  const id = page.insights[0]?.insight_id;
  return id ? `/insights/${encodeURIComponent(id)}` : null;
}

/** The latest investigation that finished, which has results to show, or the
 *  latest of any when none has. */
async function latestRunPage(client: QueryClient): Promise<string | null> {
  const runs = await client.fetchQuery(runsQuery());
  const finished = runs.filter((run) => run.status === "done");
  const latest = (finished.length > 0 ? finished : runs).reduce<Run | null>(
    (newest, run) =>
      (startedAt(run) ?? -Infinity) > (startedAt(newest) ?? -Infinity)
        ? run
        : newest,
    null,
  );
  return latest ? `/investigations/${encodeURIComponent(latest.run_id)}` : null;
}

/** The tour, in order: Home first, then one insight and one investigation in
 *  depth. Each step names a feature a new reader would otherwise have to find
 *  by clicking around. The steps on a page follow reading order, left to right
 *  and top to bottom, as Home lays them out side by side; `tourSteps` has the
 *  order for a stacked Home. */
export const TOUR_STEPS: readonly TourStep[] = [
  {
    target: "health",
    page: "/",
    title: "Is AQuA watching?",
    body:
      "Check here first. Green means AQuA is reviewing your agent on its own; " +
      "amber or red means it needs you.",
  },
  {
    target: "ask",
    page: "/",
    title: "Ask in plain words",
    body:
      "Ask why something fails, what changed, or where in your code to fix it. " +
      "AQuA can also start an investigation for you.",
  },
  {
    target: "pipeline",
    page: "/",
    title: "From traffic to insights",
    body:
      "How much of your agent's traffic AQuA reviewed, and what it found. A big " +
      "drop between two bars is worth a look; click a stage's name to explore " +
      "it.",
  },
  {
    target: "insights-strip",
    page: "/",
    title: "The worst problems first",
    body:
      "Each insight is one defect, grouped from the conversations where your " +
      "agent failed. Fix the top one and you fix the most conversations.",
  },
  {
    target: "recent-runs",
    page: "/",
    title: "Investigations are automatic",
    body:
      "AQuA investigates on a schedule and after every new revision of your " +
      "agent. Open one to see what it read and what it found.",
  },
  {
    target: "insights-triage",
    page: "/insights",
    title: "Triage the insights",
    body:
      "Merge insights that describe the same defect, and dismiss the ones you " +
      "won't act on. What's left is your to-do list.",
  },
  {
    target: "insight-evidence",
    page: topInsightPage,
    title: "The evidence",
    body:
      "Every insight comes with the real conversations behind it. Open one to " +
      "replay it turn by turn and see where it went wrong.",
  },
  {
    target: "insight-actions",
    page: topInsightPage,
    title: "Find the root cause",
    body:
      "Chat has AQuA read the code of the revision that failed and write down " +
      "the cause, with a fix. Copy as bug report hands it to a tracker or a " +
      "coding agent.",
  },
  {
    target: "run-header",
    page: latestRunPage,
    title: "Inside an investigation",
    body:
      "Everything one investigation did, start to finish. If a number looks " +
      "off, check the window it read; the narrative explains how it reached its " +
      "insights.",
  },
  {
    target: "tour-button",
    title: "Take the tour again",
    body: "It is here, on every page, whenever you need it.",
  },
];

/** The media query for the width from which Home sets Pipeline and the
 *  insights side by side, Pipeline on the left: its grid's lg breakpoint.
 *  Narrower, Home stacks them with the insights on top. */
export const HOME_SIDE_BY_SIDE = "(min-width: 1024px)";

/** The tour in the order a reader meets its steps: TOUR_STEPS when Home is
 *  side by side, and with the insights ahead of Pipeline when it is stacked, so
 *  the spotlight never climbs back up the page. */
export function tourSteps(sideBySide: boolean): readonly TourStep[] {
  if (sideBySide) return TOUR_STEPS;
  const steps = [...TOUR_STEPS];
  const pipeline = steps.findIndex((step) => step.target === "pipeline");
  const insights = steps.findIndex((step) => step.target === "insights-strip");
  [steps[pipeline], steps[insights]] = [steps[insights], steps[pipeline]];
  return steps;
}
