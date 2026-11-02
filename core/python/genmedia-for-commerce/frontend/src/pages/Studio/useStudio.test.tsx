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

import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { sendStudioAction, type StudioAction } from "../../api/studio";
import {
  FEATURED,
  resetStudioMock,
  STUDIO_GALLERY,
} from "../../test/studioMock";
import { server, setupMockBackend, stubLocalStorage } from "../../test/utils";
import { POLLING } from "./jobs";
import { blank, fromServer, missingInput, variationsOf } from "./model";
import { useStudio } from "./useStudio";

setupMockBackend();

const ID = "studio123abc";
const [EMMA] = STUDIO_GALLERY.models;
const JEANS = { label: "Blue jeans", ref: FEATURED[0].ref };
const interval = POLLING.intervalMs;

/** A saved studio on the mock server, the one this browser opens. */
async function seed(...actions: StudioAction[]) {
  for (const action of actions) await sendStudioAction(ID, action);
}

/** Every action POSTed to the studio (the mock still answers). */
function recordActions() {
  const events: StudioAction[] = [];
  server.use(
    http.post("/api/studio/:id/actions", async ({ request }) => {
      events.push(
        ((await request.clone().json()) as { event: StudioAction }).event,
      );
    }),
  );
  return events;
}

async function renderStudio() {
  const view = renderHook(() => useStudio());
  await waitFor(() => expect(view.result.current.ready).toBe(true));
  return view;
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

/** A request held until `release()` (the mock answers it then). */
function hold() {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { gate, release };
}

const seedTryOn = () =>
  seed(
    { set: { field: "person", item: EMMA } },
    { add: { field: "product", item: JEANS, replace_index: -1 } },
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

describe("useStudio", () => {
  it("opens a new studio on Photo Try-On and loads the saved one with its conversation", async () => {
    const fresh = await renderStudio();
    expect(fresh.result.current.view.capability).toBe("tryon");
    expect(fresh.result.current.messages).toEqual([]);
    fresh.unmount();

    await seed({ capability: "spin" });
    server.use(
      http.get("/api/studio/:id/messages", () =>
        HttpResponse.json({
          studio_id: ID,
          messages: [
            { role: "user", text: "hi" },
            { role: "agent", text: "hello" },
          ],
        }),
      ),
    );
    const { result } = await renderStudio();
    expect(result.current.view.capability).toBe("spin");
    await waitFor(() =>
      expect(result.current.messages.map((m) => m.text)).toEqual([
        "hi",
        "hello",
      ]),
    );
  });

  it("says why the saved studio cannot be loaded while it retries", async () => {
    const detail =
      "The server's Google Cloud credentials are missing or have expired.";
    server.use(
      http.get("/api/studio/:id/state", () =>
        HttpResponse.json({ detail }, { status: 503 }),
      ),
    );
    const { result } = renderHook(() => useStudio());
    await waitFor(() =>
      expect(result.current.restoreMessage).toContain(detail),
    );
    expect(result.current.restoreMessage).toContain("retrying");
    expect(result.current.ready).toBe(false);
  });

  it("shows only states newer than the one shown", async () => {
    server.use(
      http.get("/api/studio/:id/state", () =>
        HttpResponse.json({
          studio_id: ID,
          saved: true,
          updated_at: 100,
          state: { capability: "spin" },
        }),
      ),
    );
    let answer = { state: { capability: "video_tryon" }, updated_at: 50 };
    server.use(
      http.post("/api/studio/:id/actions", () => HttpResponse.json(answer)),
    );
    const { result } = await renderStudio();

    await act(() => result.current.act({ capability: "video_tryon" }));
    expect(result.current.view.capability).toBe("spin"); // older than the state shown

    answer = { state: { capability: "tryon" }, updated_at: 200 };
    await act(() => result.current.act({ capability: "tryon" }));
    expect(result.current.view.capability).toBe("tryon");
  });

  it("sends a gallery pick as an action and keeps its thumbnail on the stored copy", async () => {
    const events = recordActions();
    const { result } = await renderStudio();

    await act(() => result.current.setSlot("person", EMMA, "gallery"));

    expect(events).toEqual([
      {
        set: { field: "person", item: { label: "Emma", ref: EMMA.ref } },
        source: "gallery",
      },
    ]);
    expect(result.current.view.person?.ref).toMatch(
      /^gs:\/\/.*studio_catalog\/m_emma/,
    );
    expect(result.current.view.person?.thumb).toBe(EMMA.thumb);
  });

  it("shows a pick at once, with its thumbnail, while its action saves", async () => {
    const { gate, release } = hold();
    const events = recordActions();
    server.use(
      http.post("/api/studio/:id/actions", async () => {
        await gate;
      }),
    );
    const { result } = await renderStudio();

    let saved!: Promise<unknown>;
    act(() => {
      saved = result.current.setSlot("person", EMMA, "gallery");
    });
    act(() => result.current.addProduct({ ...FEATURED[0] }, "catalog"));
    act(() => result.current.addProduct({ ...FEATURED[0] }, "catalog")); // already shown: not added twice

    expect(result.current.view.person).toEqual(EMMA);
    expect(result.current.view.products).toEqual([FEATURED[0]]);
    expect(result.current.saving).toBe(true);
    release();
    await act(() => saved);
    await waitFor(() => expect(result.current.saving).toBe(false));

    expect(events).toHaveLength(2);
    expect(result.current.view.person).toEqual({
      label: "Emma",
      ref: expect.stringMatching(/^gs:\/\/.*m_emma/),
      thumb: EMMA.thumb,
    });
    expect(result.current.view.products[0]).toMatchObject({
      ref: expect.stringMatching(/^gs:\/\/.*p_jeans/),
      thumb: FEATURED[0].thumb,
    });
  });

  it("takes a pick back when its action fails, and says it was not saved", async () => {
    server.use(
      http.post("/api/studio/:id/actions", () =>
        HttpResponse.json(
          { detail: "Storage is unavailable; try again." },
          { status: 503 },
        ),
      ),
    );
    const { result } = await renderStudio();

    let saved!: Promise<unknown>;
    act(() => {
      saved = result.current.setSlot("person", EMMA, "gallery");
    });
    expect(result.current.view.person?.ref).toBe(EMMA.ref);
    await act(() => saved);

    expect(result.current.notice).toBe(
      "Not saved: Storage is unavailable; try again.",
    );
    expect(result.current.view.person).toBeNull();
  });

  it("shows each view of a spin set with its own thumbnail", async () => {
    const [set] = STUDIO_GALLERY.spin;
    const views = set.refs.map((ref, i) => ({
      label: `${set.label} ${i + 1}`,
      ref,
      thumb: set.thumbs?.[i],
    }));
    const { result } = await renderStudio();

    act(() => result.current.setSpin(views, "gallery"));
    expect(result.current.view.spin.map((v) => v.thumb)).toEqual(set.thumbs);
    await waitFor(() =>
      expect(result.current.view.spin[0].ref).toMatch(/^gs:/),
    );

    expect(result.current.view.spin.map((v) => v.thumb)).toEqual(set.thumbs);
    expect(new Set(set.thumbs).size).toBe(set.refs.length);
  });

  it("shows the user message at once, then the reply and the studio the agent saved", async () => {
    const { result } = await renderStudio();

    let turn!: Promise<void>;
    act(() => {
      turn = result.current.send("Make a 360 spin of a sneaker");
    });
    expect(result.current.messages).toEqual([
      { role: "user", text: "Make a 360 spin of a sneaker" },
    ]);
    expect(result.current.agentBusy).toBe(true);
    await act(() => turn);

    expect(result.current.agentBusy).toBe(false);
    expect(result.current.messages[1]).toMatchObject({
      role: "agent",
      text: expect.stringContaining("360° Spin"),
    });
    expect(result.current.view.capability).toBe("spin");
    expect(result.current.view.spin).toHaveLength(2);
  });

  it("shows a failed agent turn as an error reply", async () => {
    server.use(
      http.post("/api/studio/:id/agent", () =>
        HttpResponse.json({ detail: "The model is busy" }, { status: 502 }),
      ),
    );
    const { result } = await renderStudio();

    await act(() => result.current.send("hello"));

    expect(result.current.messages[1]).toEqual({
      role: "agent",
      text: "The model is busy",
      error: true,
    });
    expect(result.current.agentBusy).toBe(false);
  });

  it("generates, follows the job and records its results with job_finished", async () => {
    await seed(
      { set: { field: "person", item: EMMA } },
      { add: { field: "product", item: JEANS, replace_index: -1 } },
      { settings: { num_variations: "2" }, page: "tryon" },
    );
    const events = recordActions();
    const specs: unknown[] = [];
    server.use(
      http.post("/api/studio/:id/jobs", async ({ request }) => {
        specs.push(await request.clone().json());
      }),
    );
    const { result } = await renderStudio();

    await act(() => result.current.generate());
    await waitFor(() =>
      expect(result.current.view.results.tryon).toHaveLength(2),
    );

    expect(specs[0]).toMatchObject({
      capability: "tryon",
      person: expect.stringMatching(/^gs:/),
      products: [expect.stringMatching(/^gs:/)],
    });
    expect(events.map((e) => Object.keys(e)[0])).toEqual([
      "job_started",
      "job_finished",
    ]);
    expect(result.current.view.job).toBeNull();
    expect(result.current.following).toBe(false);
    // The saved results carry no URLs: the ones seen while polling are kept.
    expect(result.current.view.results.tryon[0].url).toMatch(
      /^\/api\/media\?uri=/,
    );
  });

  it("generates Video Try-On from photos and keeps the try-on images it animated, each video naming its image", async () => {
    await seedTryOn();
    await seed(
      { capability: "video_tryon" },
      { settings: { num_variations: "2" }, page: "video_tryon" },
    );
    const specs = recordSpecs();
    const { result } = await renderStudio();

    await act(() => result.current.generate());
    await waitFor(() => expect(result.current.view.images).toHaveLength(2));

    expect(specs[0]).toMatchObject({
      capability: "video_tryon",
      tryon_image: "",
      person: expect.stringMatching(/^gs:/),
    });
    expect(specs[0]).not.toHaveProperty("source_run");
    const [one, two] = result.current.view.images;
    expect(one).toMatchObject({
      index: 0,
      passed: true,
      url: expect.stringMatching(/^\/api\/media\?uri=/),
    });
    expect(
      result.current.view.results.video_tryon.map((r) => r.source),
    ).toEqual([one.uri, one.uri, two.uri, two.uri]);
  });

  it("makes the video of one try-on image again, replacing only that image's videos", async () => {
    await seedTryOn();
    await seed(
      { capability: "video_tryon" },
      {
        settings: { num_variations: "2", resolution: "1080p" },
        page: "video_tryon",
      },
    );
    const specs = recordSpecs();
    const { result } = await renderStudio();
    await act(() => result.current.generate());
    await waitFor(() =>
      expect(result.current.view.results.video_tryon).toHaveLength(4),
    );
    const before = result.current.view.results.video_tryon;
    const [one, two] = result.current.view.images;

    await act(() => result.current.makeVideo(two.uri as string));
    await waitFor(() => expect(result.current.view.job).toBeNull());
    await waitFor(() => expect(result.current.following).toBe(false));

    expect(specs[1]).toMatchObject({
      capability: "video_tryon",
      tryon_image: two.uri,
      settings: expect.objectContaining({ resolution: "1080p" }),
    });
    const after = result.current.view.results.video_tryon;
    expect(after.slice(0, 2)).toEqual(before.slice(0, 2)); // image 1's videos are untouched
    expect(after.slice(2).map((r) => r.source)).toEqual([two.uri, two.uri]);
    expect(after.slice(2).map((r) => r.uri)).not.toEqual(
      before.slice(2).map((r) => r.uri),
    );
    expect(after.map((r) => r.index)).toEqual([0, 1, 4, 5]);
    expect(result.current.view.images.map((r) => r.uri)).toEqual([
      one.uri,
      two.uri,
    ]);
  });

  it("tries again with the same Make video spec when it fails to start", async () => {
    await seed({ capability: "video_tryon" });
    const specs = recordSpecs();
    const { result } = await renderStudio();

    await act(() => result.current.makeVideo("gs://elsewhere/image.png"));
    expect(result.current.jobError).toBe(
      "Choose one of this studio's video try-on images.",
    );
    expect(result.current.retry).toMatchObject({
      capability: "video_tryon",
      tryon_image: "gs://elsewhere/image.png",
    });

    await act(() => result.current.tryAgain());
    expect(specs).toHaveLength(2);
    expect(specs[1]).toEqual({ ...specs[0], refresh_cache: true });
  });

  it("tries again with the spec of a Photo Try-On that ended in error, not with changed inputs", async () => {
    await seedTryOn();
    let fail = true;
    server.use(
      http.get("/api/studio/jobs/:jobId", ({ params }) =>
        fail
          ? HttpResponse.json({
              job_id: params.jobId,
              status: "error",
              error: "interrupted: the server restarted",
            })
          : undefined,
      ),
    );
    const specs = recordSpecs();
    const { result } = await renderStudio();
    expect(result.current.retry).toBeNull();

    await act(() => result.current.generate());
    await waitFor(() =>
      expect(result.current.jobError).toBe("interrupted: the server restarted"),
    );
    await waitFor(() => expect(result.current.view.job).toBeNull());
    act(() => result.current.setSetting("tryon", "num_variations", "3"));
    fail = false;
    await act(() => result.current.tryAgain());
    await waitFor(() =>
      expect(result.current.view.results.tryon).toHaveLength(1),
    );

    expect(specs).toHaveLength(2);
    expect(specs[1]).toEqual({ ...specs[0], refresh_cache: true });
    expect(result.current.retry).toBeNull();
    expect(result.current.jobError).toBe("");
  });

  it("names what Variations counts: try-on images on Video Try-On", () => {
    expect(variationsOf(blank()).label).toBe("Variations");
    expect(
      variationsOf({ ...blank(), capability: "video_tryon" }),
    ).toMatchObject({
      label: "Try-on images",
      hint: expect.stringMatching(/2 videos/),
    });
  });

  it("reads the try-on images of a studio and the image of its Make video job, and ignores the image start", () => {
    const img = {
      index: 0,
      status: "ready",
      score: 70,
      uri: "gs://b/i.png",
      url: "https://x/i.png",
      passed: false,
    };
    const job = {
      job_id: "j1",
      capability: "video_tryon",
      tryon_image: img.uri,
    };
    const view = fromServer(
      {
        images: { video_tryon: [img] },
        source: { video_tryon: { uri: "gs://b/old.png" } },
        job,
      },
      new Map(),
    );
    expect(view.images).toEqual([img]);
    expect(view.job).toEqual({
      id: "j1",
      capability: "video_tryon",
      tryonImage: img.uri,
    });
    expect(
      fromServer(
        { source: { video_tryon: { uri: "gs://b/old.png", score: 80 } } },
        new Map(),
      ).images,
    ).toEqual([]);
    expect(
      fromServer(
        { job: { job_id: "j2", capability: "spin", tryon_image: img.uri } },
        new Map(),
      ).job,
    ).toEqual({ id: "j2", capability: "spin", tryonImage: "" });
    const old = fromServer(
      {
        capability: "video_tryon",
        video_start: "image",
        tryon_image: { label: "r", ref: "gs://b/r.png" },
      },
      new Map(),
    );
    expect(old).not.toHaveProperty("tryon_image");
    expect(old.images).toEqual([]);
    expect(missingInput(old, "video_tryon")).toBe("Choose a person first.");
  });

  it("resumes following a running job after a reload", async () => {
    const { job_id } = await (
      await fetch(`/api/studio/${ID}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: "spin", settings: {} }),
      })
    ).json();
    await seed({ job_started: { job_id, capability: "spin" } });
    const events = recordActions();

    const { result } = await renderStudio();

    await waitFor(() =>
      expect(result.current.view.results.spin).toHaveLength(1),
    );
    expect(events).toEqual([{ job_finished: { job_id } }]);
  });

  it("sends nothing until the saved conversation is shown, and shows it once", async () => {
    const { gate, release } = hold();
    server.use(
      http.get("/api/studio/:id/messages", async () => {
        await gate;
        return HttpResponse.json({
          studio_id: ID,
          messages: [
            { role: "user", text: "hi" },
            { role: "agent", text: "hello" },
          ],
        });
      }),
    );
    const { result } = await renderStudio();
    expect(result.current.messagesLoaded).toBe(false);

    await act(() => result.current.send("hi"));
    expect(result.current.messages).toEqual([]);

    release();
    await waitFor(() => expect(result.current.messagesLoaded).toBe(true));
    expect(result.current.messages.map((m) => m.text)).toEqual(["hi", "hello"]);
  });

  it("shows the server's state again when an action fails", async () => {
    let reads = 0;
    server.use(
      http.get("/api/studio/:id/state", () => {
        reads++;
        return HttpResponse.json({
          studio_id: ID,
          saved: true,
          updated_at: 100,
          state: { capability: "spin" },
        });
      }),
    );
    server.use(
      http.post("/api/studio/:id/actions", () =>
        HttpResponse.json(
          { detail: "Storage is unavailable; try again." },
          { status: 503 },
        ),
      ),
    );
    const { result } = await renderStudio();

    act(() => result.current.setCapability("video_tryon"));
    expect(result.current.view.capability).toBe("video_tryon");

    await waitFor(() => expect(reads).toBe(2));
    await waitFor(() => expect(result.current.view.capability).toBe("spin"));
    expect(result.current.notice).toBe(
      "Not saved: Storage is unavailable; try again.",
    );
  });

  it("renders only the answer of the last queued action", async () => {
    let at = 100;
    server.use(
      http.post("/api/studio/:id/actions", async ({ request }) => {
        const { event } = (await request.json()) as { event: StudioAction };
        return HttpResponse.json({
          state: { capability: event.capability },
          updated_at: ++at,
        });
      }),
    );
    const seen = new Set<string>();
    const { result } = renderHook(() => {
      const studio = useStudio();
      seen.add(studio.view.capability);
      return studio;
    });
    await waitFor(() => expect(result.current.ready).toBe(true));

    await act(() =>
      Promise.all([
        result.current.act({ capability: "video_tryon" }),
        result.current.act({ capability: "spin" }),
      ]),
    );

    expect(result.current.view.capability).toBe("spin");
    expect(seen.has("video_tryon")).toBe(false);
    expect(result.current.saving).toBe(false);
  });

  it("starts one generation however often Generate is pressed", async () => {
    await seedTryOn();
    let starts = 0;
    server.use(
      http.post("/api/studio/:id/jobs", () => {
        starts++;
      }),
    );
    const events = recordActions();
    const { result } = await renderStudio();

    let first!: Promise<void>;
    act(() => {
      first = result.current.generate();
    });
    expect(result.current.starting).toBe(true);
    await act(() => Promise.all([first, result.current.generate()]));
    await waitFor(() =>
      expect(result.current.view.results.tryon).toHaveLength(1),
    );

    expect(starts).toBe(1);
    expect(events.map((e) => Object.keys(e)[0])).toEqual([
      "job_started",
      "job_finished",
    ]);
    expect(result.current.starting).toBe(false);
  });

  it('leaves a generation started before "New" with the studio it started in', async () => {
    await seedTryOn();
    const { gate, release } = hold();
    server.use(
      http.post("/api/studio/:id/jobs", async () => {
        await gate;
      }),
    );
    const events = recordActions();
    const { result } = await renderStudio();

    let start!: Promise<void>;
    act(() => {
      start = result.current.generate();
    });
    act(() => result.current.reset());
    release();
    await act(() => start);

    expect(events).toEqual([]);
    expect(result.current.view.job).toBeNull();
    expect(result.current.following).toBe(false);
    expect(result.current.starting).toBe(false);
    expect(result.current.jobError).toBe("");
  });

  it("shows why a generation failed and records its end", async () => {
    await seedTryOn();
    server.use(
      http.get("/api/studio/jobs/:jobId", ({ params }) =>
        HttpResponse.json({
          job_id: params.jobId,
          status: "error",
          error: "interrupted: the server restarted",
        }),
      ),
    );
    const events = recordActions();
    const { result } = await renderStudio();

    await act(() => result.current.generate());

    await waitFor(() =>
      expect(events.map((e) => Object.keys(e)[0])).toEqual([
        "job_started",
        "job_finished",
      ]),
    );
    expect(result.current.jobError).toBe("interrupted: the server restarted");
    expect(result.current.following).toBe(false);
  });

  it("saves a setting being typed to the old studio on reset", async () => {
    const saved: { studio: string; event: StudioAction }[] = [];
    server.use(
      http.post("/api/studio/:id/actions", async ({ params, request }) => {
        saved.push({
          studio: String(params.id),
          event: ((await request.clone().json()) as { event: StudioAction })
            .event,
        });
      }),
    );
    const { result } = await renderStudio();

    act(() =>
      result.current.setSetting("tryon", "scenario", "a beach", 60_000),
    );
    act(() => result.current.reset());

    await waitFor(() =>
      expect(saved).toEqual([
        {
          studio: ID,
          event: { settings: { scenario: "a beach" }, page: "tryon" },
        },
      ]),
    );
    expect(result.current.view.settings.tryon.scenario).not.toBe("a beach");
  });

  it("starts a new studio and conversation on reset", async () => {
    const { result } = await renderStudio();
    await act(() => result.current.send("Make a 360 spin of a sneaker"));

    act(() => result.current.reset());

    expect(result.current.studioId).not.toBe(ID);
    expect(result.current.studioId).toMatch(/^[0-9a-f]{12}$/);
    expect(localStorage.getItem("genmedia.studioId")).toBe(
      result.current.studioId,
    );
    expect(result.current.messages).toEqual([]);
    expect(result.current.view.capability).toBe("tryon");
    expect(result.current.view.spin).toEqual([]);
  });
});
