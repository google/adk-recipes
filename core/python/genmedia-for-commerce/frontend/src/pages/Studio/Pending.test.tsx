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

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { sendStudioAction, type StudioAction } from "../../api/studio";
import {
  FEATURED,
  resetStudioMock,
  STUDIO_GALLERY,
} from "../../test/studioMock";
import {
  renderAt,
  server,
  setupMockBackend,
  stubLocalStorage,
} from "../../test/utils";
import { POLLING } from "./jobs";
import Studio from ".";

setupMockBackend();

const ID = "studio123abc";
const [EMMA] = STUDIO_GALLERY.models;
const JEANS = { label: "Blue jeans", ref: FEATURED[0].ref };
const interval = POLLING.intervalMs;
const media = (uri: string) => `/api/media?uri=${encodeURIComponent(uri)}`;
const result = (uri: string, view = "front", index = 0) => ({
  index,
  status: "ready",
  score: 80,
  uri,
  url: media(uri),
  view,
});

/** Generate the page set by `saved`; the job's status polls answer `status()` (running unless it says). */
async function generate(
  status: () => Record<string, unknown>,
  ...saved: StudioAction[]
) {
  for (const action of [
    { set: { field: "person", item: EMMA } },
    { add: { field: "product", item: JEANS, replace_index: -1 } },
    ...saved,
  ])
    await sendStudioAction(ID, action);
  server.use(
    http.get("/api/studio/jobs/:jobId", ({ params }) =>
      HttpResponse.json({
        job_id: params.jobId,
        status: "running",
        progress: "Generating",
        ...status(),
      }),
    ),
  );
  renderAt("/", { "/": <Studio /> });
  fireEvent.click(await screen.findByRole("button", { name: /^Generate / }));
}

const pending = (root: HTMLElement = document.body) =>
  within(root).queryAllByRole("status", { name: "Generating…" });
const imagesRow = () => screen.getByRole("region", { name: "Try-on images" });

beforeEach(() => {
  resetStudioMock();
  stubLocalStorage();
  localStorage.setItem("genmedia.studioId", ID);
  POLLING.intervalMs = 5;
});
afterEach(() => {
  POLLING.intervalMs = interval;
});

describe("Results still coming", () => {
  it("shows a card per Photo Try-On variation still coming, before the first one too, and none once done", async () => {
    let st: Record<string, unknown> = { expected: { results: 3 } };
    await generate(() => st);

    await waitFor(() => expect(pending()).toHaveLength(3));
    expect(
      screen.getByRole("heading", { name: "Results so far" }),
    ).toBeTruthy();
    expect(screen.queryAllByRole("article")).toHaveLength(0);

    st = { ...st, results: [result("gs://b/runs/a/0.png")] };
    await waitFor(() => expect(pending()).toHaveLength(2));
    expect(screen.getAllByRole("article")).toHaveLength(1);

    st = { ...st, failed: { results: 1 } };
    await waitFor(() => expect(pending()).toHaveLength(1));
    expect(screen.getByText("1 image failed")).toBeTruthy();

    st = { ...st, status: "done" };
    await waitFor(() => expect(pending()).toHaveLength(0));
  });

  it("counts Video Try-On's try-on images, then its videos, leaving out back views", async () => {
    const [front, back] = ["gs://b/runs/a/image-0.png", "gs://b/runs/a/b0.png"];
    let st: Record<string, unknown> = {
      expected: { image_results: 2, results: 0 },
      image_results: [result(front), result(back, "back")],
    };
    await generate(() => st, { capability: "video_tryon" });

    await waitFor(() => expect(pending(imagesRow())).toHaveLength(1));
    expect(pending()).toHaveLength(1); // no video expected yet

    st = {
      expected: { image_results: 2, results: 2 },
      image_results: [result(front), result(back, "back")],
      failed: { image_results: 1 },
      images: [{ ...result(front), passed: true }],
      results: [
        { ...result("gs://b/runs/a/0.mp4"), source: front },
        { ...result("gs://b/runs/a/0b.mp4", "back"), source: front },
      ],
    };
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    expect(pending(imagesRow())).toHaveLength(0);
    expect(within(imagesRow()).getByText("1 image failed")).toBeTruthy();
    expect(pending()).toHaveLength(1);

    st = { ...st, failed: { image_results: 1, results: 2 } };
    await waitFor(() =>
      expect(screen.getByText("2 videos failed")).toBeTruthy(),
    );
    expect(pending()).toHaveLength(0);
  });

  it("shows none when the server does not say how many are coming", async () => {
    await generate(() => ({ results: [result("gs://b/runs/a/0.png")] }));

    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));
    expect(
      screen.getByRole("heading", { name: "Results so far" }),
    ).toBeTruthy();
    expect(pending()).toHaveLength(0);
  });

  it("keeps each result's element when the results are ranked again", async () => {
    const [a, b] = ["gs://b/runs/a/0.png", "gs://b/runs/a/1.png"];
    let st: Record<string, unknown> = {
      results: [result(a), result(b, "front", 1)],
    };
    await generate(() => st);
    const img = await screen.findByRole("img", { name: "Variation 1" });
    expect(img.getAttribute("src")).toBe(media(a));

    st = { results: [result(b, "front", 1), result(a)] };
    await waitFor(() =>
      expect(screen.getAllByRole("article")[1].contains(img)).toBe(true),
    );
  });
});
