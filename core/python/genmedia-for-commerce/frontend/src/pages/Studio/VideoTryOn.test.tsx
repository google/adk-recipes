/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

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

async function renderStudio(...saved: StudioAction[]) {
  for (const action of saved) await sendStudioAction(ID, action);
  renderAt("/", { "/": <Studio /> });
  await waitFor(() =>
    expect(
      screen.getByRole("region", { name: "Inputs" }).getAttribute("aria-busy"),
    ).toBe("false"),
  );
}

/** Every job spec POSTed (the mock still answers). */
function recordSpecs() {
  const specs: Record<string, unknown>[] = [];
  server.use(
    http.post("/api/studio/:id/jobs", async ({ request }) => {
      specs.push((await request.clone().json()) as Record<string, unknown>);
    }),
  );
  return specs;
}

const imagesRow = () => screen.getByRole("region", { name: "Try-on images" });
const imageCard = (n: number) =>
  within(imagesRow()).getAllByRole("listitem")[n - 1];
const videos = () => screen.getAllByRole("article");
const fromImage = (n: number) =>
  videos().filter((v) => within(v).queryByText(`From image ${n}`));
const names = (cards: HTMLElement[]) =>
  cards.map((v) => v.querySelector("video")?.getAttribute("aria-label"));
const best = () => videos().filter((v) => within(v).queryByText("Best"));
const seedVideoTryOn = () =>
  renderStudio(
    { set: { field: "person", item: EMMA } },
    { add: { field: "product", item: JEANS, replace_index: -1 } },
    { capability: "video_tryon" },
    { settings: { num_variations: "2" }, page: "video_tryon" },
  );

beforeEach(() => {
  resetStudioMock();
  stubLocalStorage();
  localStorage.setItem("genmedia.studioId", ID);
  POLLING.intervalMs = 5;
});
afterEach(() => {
  POLLING.intervalMs = interval;
});

describe("Video Try-On results", () => {
  it("shows the try-on images with their score, videos and a badge when the image did not pass", async () => {
    const [good, best] = [
      "gs://b/runs/a/image-0.png",
      "gs://b/runs/a/image-1.png",
    ];
    const state = {
      capability: "video_tryon",
      images: {
        video_tryon: [
          {
            index: 0,
            status: "ready",
            score: 91,
            uri: good,
            url: media(good),
            passed: true,
          },
          {
            index: 1,
            status: "rejected",
            score: 64,
            uri: best,
            url: "javascript:alert(1)",
            passed: false,
          },
        ],
      },
      results: {
        video_tryon: [
          {
            index: 0,
            status: "ready",
            score: 80,
            uri: "gs://b/runs/a/0.mp4",
            url: media("gs://b/runs/a/0.mp4"),
            source: good,
          },
        ],
      },
    };
    server.use(
      http.get("/api/studio/:id/state", () =>
        HttpResponse.json({ studio_id: ID, saved: true, updated_at: 1, state }),
      ),
    );
    await renderStudio();

    const one = imageCard(1);
    expect(within(one).getByText("Image 1")).toBeTruthy();
    expect(within(one).getByText("Score 91")).toBeTruthy();
    expect(within(one).getByText("1 video")).toBeTruthy();
    expect(within(one).queryByText("Didn't pass every check")).toBeNull();
    expect(
      within(one).getByRole("img", { name: "Image 1" }).getAttribute("src"),
    ).toBe(media(good));
    const two = imageCard(2);
    expect(within(two).getByText("Didn't pass every check")).toBeTruthy();
    expect(within(two).getByText("No video")).toBeTruthy();
    expect(within(two).getByText("Preview unavailable")).toBeTruthy(); // its unsafe url is not loaded
    expect(
      (
        within(two).getByRole("button", {
          name: "Make video from Image 2",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(within(videos()[0]).getByText("From image 1")).toBeTruthy();
  });

  it("links each video to its image and remakes one image's videos, live, leaving the others", async () => {
    await seedVideoTryOn();
    const specs = recordSpecs();
    fireEvent.click(
      screen.getByRole("button", { name: "Generate Video Try-On" }),
    );
    await waitFor(() =>
      expect(within(imagesRow()).getAllByRole("listitem")).toHaveLength(2),
    );
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Videos" })).toBeTruthy(),
    );
    expect(fromImage(1)).toHaveLength(2);
    expect(fromImage(2)).toHaveLength(2);
    expect(best()).toEqual([videos()[0]]); // the judge's best of the run
    const kept = fromImage(1).map((v) =>
      v.querySelector("video")?.getAttribute("src"),
    );

    // Hovering a video highlights its image.
    fireEvent.mouseEnter(fromImage(2)[0]);
    expect(imageCard(2).className).toContain("ring-2");
    expect(imageCard(1).className).not.toContain("ring-2");
    fireEvent.mouseLeave(fromImage(2)[0]);

    // The Make video job runs until released, with one new video so far.
    let release = false;
    server.use(
      http.get("/api/studio/jobs/:jobId", ({ params }) => {
        if (release) return undefined;
        const uri = "gs://b/runs/remake/0.mp4";
        return HttpResponse.json({
          job_id: params.jobId,
          status: "running",
          progress: "Animating",
          images: [],
          tryon_image: specs[1].tryon_image,
          results: [
            {
              index: 0,
              status: "ready",
              score: 77,
              uri,
              url: media(uri),
              source: specs[1].tryon_image,
            },
          ],
        });
      }),
    );
    fireEvent.click(
      within(imageCard(2)).getByRole("button", {
        name: "Make video from Image 2",
      }),
    );

    await waitFor(() =>
      expect(within(imageCard(2)).getByText("Making video…")).toBeTruthy(),
    );
    expect(specs[1]).toMatchObject({
      capability: "video_tryon",
      tryon_image: expect.stringMatching(/image-1\.png$/),
    });
    await waitFor(() => expect(fromImage(2)).toHaveLength(1));
    // Last, numbered after the saved videos (0 to 3), as the server saves it.
    expect(names(videos())).toEqual([
      "Variation 1 video",
      "Variation 2 video",
      "Variation 5 video",
    ]);
    expect(
      fromImage(1).map((v) => v.querySelector("video")?.getAttribute("src")),
    ).toEqual(kept);
    expect(within(imagesRow()).getAllByRole("listitem")).toHaveLength(2);
    expect(
      (
        within(imageCard(1)).getByRole("button", {
          name: "Make video from Image 1",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Generating…" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    release = true;
    await waitFor(() =>
      expect(within(imageCard(2)).getByText("2 videos")).toBeTruthy(),
    );
    expect(names(videos())).toEqual([
      "Variation 1 video",
      "Variation 2 video",
      "Variation 5 video",
      "Variation 6 video",
    ]);
    expect(
      fromImage(1).map((v) => v.querySelector("video")?.getAttribute("src")),
    ).toEqual(kept);
    expect(best()).toEqual([]); // its new videos were never compared with the others
    expect(
      (
        within(imageCard(1)).getByRole("button", {
          name: "Make video from Image 1",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });

  it("follows a Make video job the studio is running (after a reload, or the agent's) as a remake of its image", async () => {
    const [one, two] = [
      "gs://b/runs/a/outputs/image-0.png",
      "gs://b/runs/a/outputs/image-1.png",
    ];
    const image = (index: number, uri: string) => ({
      index,
      status: "ready",
      score: 90,
      uri,
      url: media(uri),
      passed: true,
    });
    const video = (index: number, source: string) => {
      const uri = `gs://b/runs/a/outputs/${index}.mp4`;
      return {
        index,
        status: "ready",
        score: 80,
        uri,
        url: media(uri),
        source,
      };
    };
    const state = {
      capability: "video_tryon",
      images: { video_tryon: [image(0, one), image(1, two)] },
      results: { video_tryon: [video(0, one), video(1, two)] },
      job: { job_id: "remake1", capability: "video_tryon", tryon_image: two },
    };
    server.use(
      http.get("/api/studio/:id/state", () =>
        HttpResponse.json({ studio_id: ID, saved: true, updated_at: 1, state }),
      ),
      // A status without its image yet: the saved job names it.
      http.get("/api/studio/jobs/:jobId", ({ params }) =>
        HttpResponse.json({
          job_id: params.jobId,
          status: "running",
          progress: "Animating",
          results: [],
        }),
      ),
    );
    await renderStudio();

    await waitFor(() =>
      expect(within(imageCard(2)).getByText("Making video…")).toBeTruthy(),
    );
    expect(within(imagesRow()).getAllByRole("listitem")).toHaveLength(2);
    expect(fromImage(1)).toHaveLength(1);
    expect(fromImage(2)).toHaveLength(0);
  });
});

describe("Try again", () => {
  it("starts the same Photo Try-On again after it ended in error", async () => {
    await renderStudio(
      { set: { field: "person", item: EMMA } },
      { add: { field: "product", item: JEANS, replace_index: -1 } },
    );
    let fail = true;
    server.use(
      http.get("/api/studio/jobs/:jobId", ({ params }) =>
        fail
          ? HttpResponse.json({
              job_id: params.jobId,
              status: "error",
              error: "The model is busy",
            })
          : undefined,
      ),
    );
    const specs = recordSpecs();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "Generate Photo Try-On" }),
    );

    expect((await screen.findByRole("alert")).textContent).toBe(
      "The model is busy",
    );
    const again = await screen.findByRole("button", { name: "Try again" });
    await waitFor(() =>
      expect((again as HTMLButtonElement).disabled).toBe(false),
    );
    fail = false;
    fireEvent.click(again);

    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));
    expect(specs).toHaveLength(2);
    expect(specs[1]).toEqual({ ...specs[0], refresh_cache: true });
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("starts the same Make video again after it ended in error", async () => {
    await seedVideoTryOn();
    fireEvent.click(
      screen.getByRole("button", { name: "Generate Video Try-On" }),
    );
    await waitFor(() =>
      expect(within(imagesRow()).getAllByRole("listitem")).toHaveLength(2),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Generate Video Try-On" }),
      ).toHaveProperty("disabled", false),
    );
    const specs = recordSpecs();
    server.use(
      http.get("/api/studio/jobs/:jobId", ({ params }) =>
        specs.length < 2
          ? HttpResponse.json({
              job_id: params.jobId,
              status: "error",
              error: "Video generation failed",
            })
          : undefined,
      ),
    );

    fireEvent.click(
      within(imageCard(1)).getByRole("button", {
        name: "Make video from Image 1",
      }),
    );
    const again = await screen.findByRole("button", { name: "Try again" });
    await waitFor(() =>
      expect((again as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(again);

    await waitFor(() => expect(specs).toHaveLength(2));
    expect(specs[1]).toEqual({ ...specs[0], refresh_cache: true });
    expect(specs[0]).toMatchObject({
      capability: "video_tryon",
      tryon_image: expect.stringMatching(/image-0\.png$/),
    });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull(),
    );
  });
});
