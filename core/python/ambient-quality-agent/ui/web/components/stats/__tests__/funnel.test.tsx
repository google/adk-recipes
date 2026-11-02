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

// biome-ignore-all lint/style/noNonNullAssertion: a missing value fails the test either way; the assertion only narrows the type.

import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { withStatsDefaults } from "@/lib/aqua-api";
import { STAGE_HELP, STAGE_HELP_ONE_RUN, STAGES } from "@/lib/funnel";
import { Funnel } from "../funnel";

describe("Funnel", () => {
  it("labels every stage for a reader who cannot see the bars", () => {
    render(
      <Funnel
        stats={withStatsDefaults({
          traces_scanned: 1200,
          traces_ingested: 90,
          traces_ingested_partial: 6,
          traces_ingested_failed: 4,
          traces_eval_passed: 70,
          traces_eval_failed: 25,
          traces_eval_errored: 1,
          insights_created: 3,
          insights_recurring: 2,
        })}
      />,
    );

    expect(
      screen.getByRole("img", {
        name: "Trajectories in the window: 1,200 total. 1,200 in the window",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Ingested: 100 total. 90 ingested, 6 partially ingested, 4 ingestion failed",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Evaluated: 96 total. 70 passed, 25 failed, 1 eval service errors",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Insights: 5 total. 3 new, 2 recurring",
      }),
    ).toBeInTheDocument();
  });

  it("says the funnel changes unit at the last step", () => {
    render(<Funnel stats={withStatsDefaults({ traces_scanned: 10 })} />);
    expect(
      screen.getByText(/The first three count trajectories/),
    ).toBeInTheDocument();
  });

  it("reads each figure once, through the bar's label", () => {
    render(<Funnel stats={FULL} />);
    // The img label already carries the total and every segment, so the
    // visible copies are hidden from assistive tech rather than read twice.
    for (const figure of ["1,200", "100", "90", "6", "5"]) {
      for (const el of screen.getAllByText(figure)) {
        expect(el.closest('[aria-hidden="true"]')).not.toBeNull();
      }
    }
  });

  // insights_created + insights_recurring are summed over investigations, so
  // an insight that several investigations record as new or recurring is
  // counted once by each.
  it("says the insights stage counts each investigation's new and recurring insights", () => {
    render(<Funnel stats={withStatsDefaults({ traces_scanned: 10 })} />);
    const note = screen.getByText(/The first three count trajectories/);
    expect(note).toHaveTextContent(
      /each investigation's new and recurring insights/,
    );
    expect(note).not.toHaveTextContent(/occurrences/);
    expect(note).toHaveTextContent(/not the number of distinct insights/);
    expect(note).not.toHaveTextContent(/the distinct quality insights/);
  });

  it("renders a deployment with no runs as four empty stages, not as blanks", () => {
    render(<Funnel stats={withStatsDefaults({})} />);
    expect(screen.getAllByRole("img")).toHaveLength(4);
    expect(
      screen.getByRole("img", { name: "Insights: 0 total." }),
    ).toBeInTheDocument();
  });

  it("explains each stage behind a button, with or without the links", async () => {
    const user = userEvent.setup();
    render(<Funnel stats={FULL} />);

    await user.click(
      screen.getByRole("button", { name: "About the Ingested stage" }),
    );
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(STAGE_HELP.ingested);
    for (const stage of STAGES) {
      expect(
        screen.getByRole("button", { name: `About the ${stage.label} stage` }),
      ).toBeInTheDocument();
    }
    // Outside the bar's image, like the links, so it is not flattened away.
    for (const button of screen.getAllByRole("button")) {
      expect(button.closest('[role="img"]')).toBeNull();
    }
  });

  it("links no stage unless asked to", () => {
    render(<Funnel stats={FULL} />);
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("words a funnel of one investigation as that investigation's, not a sum", async () => {
    const user = userEvent.setup();
    render(<Funnel stats={{ ...FULL, investigations: 1 }} />);

    expect(
      screen.getByText(/this investigation's new and recurring insights/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/added up/)).toBeNull();
    await user.click(
      screen.getByRole("button", {
        name: "About the Trajectories in the window stage",
      }),
    );
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(STAGE_HELP_ONE_RUN.window!);
  });

  it("words totals over several investigations as a sum", async () => {
    const user = userEvent.setup();
    render(<Funnel stats={{ ...FULL, investigations: 3 }} />);

    expect(screen.getByText(/added up/)).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "About the Trajectories in the window stage",
      }),
    );
    expect(
      await screen.findByRole("dialog", { hidden: true }),
    ).toHaveTextContent(STAGE_HELP.window);
  });

  describe("with stage links", () => {
    it("links each stage title, and nothing else, to its page", async () => {
      await renderInRouter(<Funnel stats={FULL} linkStages />);

      // One per stage: the pills, segments and bars count in other units than
      // the lists these pages show, so they are not links.
      expect(screen.getAllByRole("link")).toHaveLength(4);
      expect(
        screen.getByRole("link", { name: /Trajectories in the window/ }),
      ).toHaveAttribute("href", "/investigations");
      expect(screen.getByRole("link", { name: /Ingested/ })).toHaveAttribute(
        "href",
        "/investigations",
      );
      expect(screen.getByRole("link", { name: /Evaluated/ })).toHaveAttribute(
        "href",
        "/investigations",
      );
      expect(screen.getByRole("link", { name: /Insights/ })).toHaveAttribute(
        "href",
        "/insights",
      );
    });

    it("says in each link's name which list it opens", async () => {
      await renderInRouter(<Funnel stats={FULL} linkStages />);
      // Three stages open the same list, and a stage's title does not say
      // which list that is. The \s* is jsdom, which puts a space before the
      // hidden span that a browser does not.
      expect(
        screen.getByRole("link", {
          name: /^Ingested\s*, view investigations$/,
        }),
      ).toHaveAttribute("href", "/investigations");
      expect(
        screen.getByRole("link", { name: /^Insights\s*, view insights$/ }),
      ).toHaveAttribute("href", "/insights");
    });

    it("underlines the titles as links on hover and focus", async () => {
      await renderInRouter(<Funnel stats={FULL} linkStages />);
      for (const link of screen.getAllByRole("link")) {
        expect(link).toHaveClass("hover:underline", "focus-visible:underline");
      }
    });

    it("keeps the links out of the bars' images", async () => {
      await renderInRouter(<Funnel stats={FULL} linkStages />);
      // Children of role=img are presentational, so a link inside one can be
      // flattened away by assistive tech.
      for (const link of screen.getAllByRole("link")) {
        expect(link.closest('[role="img"]')).toBeNull();
      }
      expect(screen.getAllByRole("img")).toHaveLength(4);
    });

    it("gives every link a focus ring the bar's overflow cannot clip", async () => {
      await renderInRouter(<Funnel stats={FULL} linkStages />);
      for (const link of screen.getAllByRole("link")) {
        expect(link).toHaveClass(
          "focus-visible:outline-none",
          "focus-visible:ring-2",
          "focus-visible:ring-ring",
          "focus-visible:ring-inset",
        );
      }
    });
  });
});

const FULL = withStatsDefaults({
  traces_scanned: 1200,
  traces_ingested: 90,
  traces_ingested_partial: 6,
  traces_ingested_failed: 4,
  traces_eval_passed: 70,
  traces_eval_failed: 25,
  traces_eval_errored: 1,
  insights_created: 3,
  insights_recurring: 2,
});

async function renderInRouter(ui: React.ReactElement) {
  const root = createRootRoute();
  const routes = ["/", "/investigations", "/insights"].map((path) =>
    createRoute({
      getParentRoute: () => root,
      path,
      ...(path === "/" ? { component: () => ui } : {}),
    }),
  );
  const router = createRouter({
    routeTree: root.addChildren(routes),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  // biome-ignore lint/suspicious/noExplicitAny: the test router is not the app's registered router type.
  render(<RouterProvider router={router as any} />);
  await screen.findAllByRole("img");
}
