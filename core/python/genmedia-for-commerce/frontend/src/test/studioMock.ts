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

// Fixture Studio API (/api/studio/*, genmedia4commerce/api/studio.py) for the tests and
// `npm run dev:mock`: studios in memory, a keyword agent, and jobs whose variations arrive
// over time. Saved states drop result URLs (as the server does) and GET state renews them.
// A Video Try-On animates each of its try-on images into VIDEOS_PER_IMAGE videos; a "Make
// video" job (a `tryon_image` of the studio's images) remakes only that image's videos.
import { http, HttpResponse } from "msw";
import type { ChatMessage, StudioGallery, StudioItem } from "../api/studio";

// biome-ignore lint/suspicious/noExplicitAny: the mock's state is loose JSON, as the server's
type State = Record<string, any>;
type Doc = { state: State; updated_at: number; messages: ChatMessage[] };
/** `n`: its try-on images (Video Try-On) or variations; `tryonImage`: the image of a "Make video" job. */
type Job = {
  id: string;
  studioId: string;
  capability: string;
  n: number;
  started: number;
  tryonImage: string;
};
/** `delayMs`: the time between two variations of a job (0: a job is done at once). */
type Config = { bucket: string; delayMs: () => number };

const CAPS = ["tryon", "video_tryon", "spin"];
const LISTS: Record<string, string> = { product: "products", spin: "spin" };
const SLOTS = ["person", "reference_model"];
export const VIDEOS_PER_IMAGE = 2;
/** Settings kept on one page whichever page sets them. */
const TRYON_KEYS = [
  "scenario",
  "style_guidelines",
  "complete_look",
  "upscale_crops",
];
const studios = new Map<string, Doc>();
const jobs = new Map<string, Job>();
let clock = 0;

/** Forget every studio and job (between tests). */
export function resetStudioMock(): void {
  studios.clear();
  jobs.clear();
}

const tick = () => (clock = Math.max(Date.now() / 1000, clock + 0.001));
const doc = (id: string): Doc =>
  studios.get(id) ?? { state: {}, updated_at: 0, messages: [] };
/** `catalog:<id>` or `catalog:<id>/<n>` -> its thumbnail URL, as studio/picks.py makes it. */
const catalogThumb = (ref: string) => {
  const [id, n = "0"] = ref.slice("catalog:".length).split("/");
  return `/api/catalog/image/${id}/${n}?w=256`;
};
const mediaUrl = (uri: string) => `/api/media?uri=${encodeURIComponent(uri)}`;
const badRequest = (detail: string) =>
  HttpResponse.json({ detail }, { status: 400 });

const item = (label: string, id: string): StudioItem => ({
  label,
  ref: `catalog:${id}`,
  thumb: catalogThumb(`catalog:${id}`),
});
const spinSet = (label: string, id: string, views: number) => {
  const refs = Array.from(
    { length: views },
    (_, n) => `catalog:${id}${n ? `/${n}` : ""}`,
  );
  return {
    label,
    refs,
    thumb: catalogThumb(refs[0]),
    thumbs: refs.map(catalogThumb),
  };
};
export const STUDIO_GALLERY: StudioGallery = {
  models: [item("Emma", "m_emma"), item("Liam", "m_liam")],
  spin: [spinSet("White sneaker", "s_white_sneaker", 2)],
};
/** The featured products (an empty catalogue search) and the rest of the fixture catalogue. */
export const FEATURED: StudioItem[] = [
  item("Blue jeans", "p_jeans"),
  item("Black leather jacket", "p_jacket"),
];
const OTHERS: StudioItem[] = [
  item("Red linen shirt", "p1"),
  item("Red wool scarf", "p2"),
  item("Grey hoodie", "p3"),
];

/** Apply one action or patch (the studio/patches.py rules, simplified). */
function apply(st: State, p: State): void {
  if (CAPS.includes(p.capability)) st.capability = p.capability;
  for (const [k, v] of Object.entries(p.settings ?? {})) {
    const cap = TRYON_KEYS.includes(k)
      ? "tryon"
      : ["motion", "prompt"].includes(k)
        ? "video_tryon"
        : p.page || st.capability || "tryon";
    st.settings ??= {};
    st.settings[cap] ??= {};
    st.settings[cap][k] = v;
  }
  const { set, add, remove, clear } = p;
  if (set && LISTS[set.field] && Array.isArray(set.items))
    st[LISTS[set.field]] = set.items.slice(0, 8);
  else if (set && SLOTS.includes(set.field) && set.item)
    st[set.field] = set.item;
  if (add && LISTS[add.field]) {
    st[LISTS[add.field]] ??= [];
    const list = st[LISTS[add.field]];
    if (add.replace_index >= 0 && add.replace_index < list.length)
      list[add.replace_index] = add.item;
    else if (list.length < 8) list.push(add.item);
  }
  if (remove && LISTS[remove.field])
    st[LISTS[remove.field]]?.splice(remove.index, 1);
  if (clear && LISTS[clear.field]) st[LISTS[clear.field]] = [];
  else if (clear && SLOTS.includes(clear.field)) st[clear.field] = null;
  if (p.job_started && CAPS.includes(p.job_started.capability)) {
    const cap = p.job_started.capability;
    const image = jobs.get(p.job_started.job_id)?.tryonImage;
    st.job = {
      job_id: p.job_started.job_id,
      capability: cap,
      ...(image ? { tryon_image: image } : {}),
    };
    // A full run replaces the page's results; a "Make video" job keeps them until it ends.
    if (!image) {
      st.results ??= {};
      st.results[cap] = [];
    }
  }
}

function save(id: string, change: (st: State) => void): Doc {
  const current = doc(id);
  const state = structuredClone(current.state);
  change(state);
  const next = { ...current, state, updated_at: tick() };
  studios.set(id, next);
  return next;
}

/** Catalogue picks become gs:// copies, as the server stores them: `{catalogue ref: copy}`. */
function copyCatalog(bucket: string, items: State[]): Record<string, string> {
  const refs: Record<string, string> = {};
  for (const it of items) {
    if (typeof it?.ref !== "string" || !it.ref.startsWith("catalog:")) continue;
    refs[it.ref] =
      `gs://${bucket}/runs/studio_catalog/${it.ref.slice("catalog:".length)}.webp`;
    it.ref = refs[it.ref];
  }
  return refs;
}

/** The try-on images a full Video Try-On animates: all of them pass. */
function tryonImages(job: Job, folder: string) {
  if (job.capability !== "video_tryon" || job.tryonImage) return [];
  return Array.from({ length: job.n }, (_, i) => {
    const uri = `${folder}/outputs/image-${i}.png`;
    return {
      index: i,
      status: "ready",
      score: 88 - i * 4,
      uri,
      url: mediaUrl(uri),
      note: "Matches the product references.",
      run: `${folder}/manifest.json`,
      passed: true,
    };
  });
}

/** A job's status: one more variation every `delayMs`, then done (Video Try-On's images from the start). */
function jobStatus(job: Job, { bucket, delayMs }: Config) {
  const ext =
    job.capability === "tryon"
      ? "png"
      : job.capability === "spin"
        ? "webp"
        : "mp4"; // a spin is an animated WebP
  const folder = `gs://${bucket}/runs/2026-10-08/studio-${job.studioId}/${job.id}`;
  const images = tryonImages(job, folder);
  const total = job.tryonImage
    ? VIDEOS_PER_IMAGE
    : images.length
      ? images.length * VIDEOS_PER_IMAGE
      : job.n;
  const delay = delayMs();
  const shown = delay
    ? Math.min(total, Math.floor((Date.now() - job.started) / delay))
    : total;
  const results = Array.from({ length: shown }, (_, i) => {
    const uri = `${folder}/outputs/${i}.${ext}`;
    const ok = i !== 2;
    const source =
      job.tryonImage || images[Math.floor(i / VIDEOS_PER_IMAGE)]?.uri;
    return {
      index: i,
      status: ok ? "ready" : "rejected",
      score: 90 - i * 15,
      uri,
      url: mediaUrl(uri),
      view: "front",
      run: `${folder}/manifest.json`,
      note: ok
        ? "Matches the product references."
        : "Logo and buttons differ from the reference.",
      ...(source ? { source } : {}),
    };
  });
  const done = shown >= total;
  return {
    job_id: job.id,
    status: done ? "done" : "running",
    capability: job.capability,
    progress: done ? "Done" : `Generating variation ${shown + 1} of ${total}`,
    elapsed_s: Math.round((Date.now() - job.started) / 1000),
    results,
    images,
    tryon_image: job.tryonImage,
  };
}

function startJob(studioId: string, spec: State): Job {
  const job = {
    id: crypto.randomUUID().replace(/-/g, "").slice(0, 10),
    studioId,
    capability: spec.capability,
    n: Number(spec.settings?.num_variations ?? 1) || 1,
    started: Date.now(),
    tryonImage:
      spec.capability === "video_tryon" ? String(spec.tryon_image ?? "") : "",
  };
  jobs.set(job.id, job);
  return job;
}

/**
 * The results a finished job leaves in the studio, without URLs (as studio/state.py stores them): a full run
 * replaces the page's (and Video Try-On's images); a "Make video" job only its image's videos, numbered after the rest.
 */
function finish(st: State, job: Job, config: Config): void {
  if (st.job?.job_id !== job.id) return;
  const status = jobStatus(job, config);
  if (status.status !== "done") return;
  const bare = ({ url, ...rest }: State) => {
    void url;
    return rest;
  };
  st.job = null;
  st.results ??= {};
  const results = st.results;
  if (!job.tryonImage) {
    results[job.capability] = status.results.map(bare);
    if (status.images.length) {
      st.images ??= {};
      st.images[job.capability] = status.images.map(bare);
    }
    return;
  }
  const kept = (results.video_tryon ?? []).filter(
    (r: State) => r.source !== job.tryonImage,
  );
  const top = Math.max(
    -1,
    ...(results.video_tryon ?? []).map((r: State) => Number(r.index) || 0),
  );
  results.video_tryon = [
    ...kept,
    ...status.results.map((r, i) => ({ ...bare(r), index: top + 1 + i })),
  ];
}

/** The keyword agent: prefills a try-on or a spin, or starts the page's generation. */
function agentReply(
  st: State,
  message: string,
  studioId: string,
  config: Config,
): { reply: string; change?: (st: State) => void } {
  const text = message.toLowerCase();
  if (text.includes("spin")) {
    const shoe = STUDIO_GALLERY.spin[0];
    const items = shoe.refs.map((ref, i) => ({
      label: `${shoe.label} ${i + 1}`,
      ref,
    }));
    copyCatalog(config.bucket, items);
    return {
      reply: `I opened 360° Spin with the ${shoe.label.toLowerCase()}. Press Generate when you are ready.`,
      change: (s) =>
        apply(s, { capability: "spin", set: { field: "spin", items } }),
    };
  }
  if (/vto|try/.test(text)) {
    const [person] = STUDIO_GALLERY.models;
    const items = [
      { label: person.label, ref: person.ref },
      { label: FEATURED[1].label, ref: FEATURED[1].ref },
    ];
    copyCatalog(config.bucket, items);
    return {
      reply: `${person.label} is set as the person, wearing the ${items[1].label.toLowerCase()}. Say "generate" or press Generate.`,
      change: (s) => {
        apply(s, {
          capability: "tryon",
          set: { field: "person", item: items[0] },
        });
        apply(s, { set: { field: "product", items: [items[1]] } });
      },
    };
  }
  if (/generate|run|go/.test(text) && (st.person || st.spin?.length)) {
    const cap = st.capability ?? "tryon";
    const job = startJob(studioId, {
      capability: cap,
      settings: st.settings?.[cap],
    });
    return {
      reply: "Started the generation; the results appear in the studio.",
      change: (s) =>
        apply(s, { job_started: { job_id: job.id, capability: cap } }),
    };
  }
  return {
    reply:
      "I can set up a Photo Try-On, a Video Try-On or a 360° Spin, and run it. What would you like to make?",
  };
}

/** The routes of genmedia4commerce/api/studio.py. */
export function studioHandlers(config: Config) {
  return [
    http.get("/api/studio/gallery", () => HttpResponse.json(STUDIO_GALLERY)),
    http.post("/api/studio/previews", async ({ request }) => {
      const { refs } = (await request.json()) as { refs: string[] };
      const previews = Object.fromEntries(
        refs.flatMap((ref) =>
          ref.startsWith("catalog:")
            ? [[ref, catalogThumb(ref)]]
            : ref.startsWith(`gs://${config.bucket}/runs/`)
              ? [[ref, mediaUrl(ref)]]
              : [],
        ),
      );
      return HttpResponse.json({ previews });
    }),
    http.get("/api/studio/jobs/:jobId", ({ params }) => {
      const job = jobs.get(String(params.jobId));
      return HttpResponse.json(
        job
          ? jobStatus(job, config)
          : { job_id: params.jobId, status: "unknown" },
      );
    }),
    http.get("/api/studio/:id/state", ({ params }) => {
      const { state, updated_at } = doc(String(params.id));
      const renewed = structuredClone(state);
      for (const key of ["results", "images"]) {
        for (const list of Object.values(renewed[key] ?? {}) as State[][])
          list.forEach((r) => {
            r.url = mediaUrl(r.uri);
          });
      }
      return HttpResponse.json({
        studio_id: params.id,
        saved: Object.keys(state).length > 0,
        updated_at,
        state: renewed,
      });
    }),
    http.get("/api/studio/:id/messages", ({ params }) =>
      HttpResponse.json({
        studio_id: params.id,
        messages: doc(String(params.id)).messages,
      }),
    ),
    http.post("/api/studio/:id/actions", async ({ params, request }) => {
      const id = String(params.id);
      const { event } = (await request.json()) as { event: State };
      if (event.job_finished) {
        const job = jobs.get(event.job_finished.job_id);
        if (!job) return badRequest("job_finished needs a job_id.");
        const { state, updated_at } = save(id, (st) => finish(st, job, config));
        return HttpResponse.json({ state, updated_at });
      }
      const refs = copyCatalog(config.bucket, [
        event.set?.item,
        ...(event.set?.items ?? []),
        event.add?.item,
      ]);
      const { state, updated_at } = save(id, (st) => apply(st, event));
      return HttpResponse.json({ state, updated_at, refs });
    }),
    http.post("/api/studio/:id/agent", async ({ params, request }) => {
      const id = String(params.id);
      const { message } = (await request.json()) as { message: string };
      const { reply, change } = agentReply(doc(id).state, message, id, config);
      const saved = save(id, (st) => change?.(st));
      saved.messages = [
        ...saved.messages,
        { role: "user", text: message },
        { role: "agent", text: reply },
      ];
      const turn = {
        studio_id: id,
        reply,
        patches: [],
        issued_at: saved.updated_at,
      };
      return HttpResponse.json(
        change
          ? { ...turn, state: saved.state, updated_at: saved.updated_at }
          : turn,
      );
    }),
    http.post("/api/studio/:id/uploads", async ({ params, request }) => {
      const filename = (await request.text()).match(/filename="([^"]*)"/)?.[1];
      if (filename === undefined)
        return badRequest("file: not a PNG, JPEG or WebP image");
      const ref = `gs://${config.bucket}/runs/2026-10-08/studio-${params.id}/inputs/upload-${crypto.randomUUID().slice(0, 8)}.png`;
      return HttpResponse.json({
        ref,
        label: filename || "Upload",
        thumb: mediaUrl(ref),
        description: "A photo of a person in a plain studio.",
      });
    }),
    http.post("/api/studio/:id/catalog-search", async ({ request }) => {
      const query = ((await request.json()) as { query: string }).query.trim();
      const words = query.toLowerCase().split(/\s+/);
      const items = query
        ? [...FEATURED, ...OTHERS].filter((it) =>
            words.some((w) => it.label.toLowerCase().includes(w)),
          )
        : FEATURED;
      return HttpResponse.json({ query, items, error: null });
    }),
    http.post("/api/studio/:id/jobs", async ({ params, request }) => {
      const spec = (await request.json()) as State;
      if (!CAPS.includes(spec.capability))
        return badRequest("capability must be tryon, video_tryon or spin");
      const images: State[] =
        doc(String(params.id)).state.images?.video_tryon ?? [];
      if (
        spec.capability === "video_tryon" &&
        spec.tryon_image &&
        !images.some((img) => img.uri === spec.tryon_image)
      ) {
        return badRequest("Choose one of this studio's video try-on images.");
      }
      const job = startJob(String(params.id), spec);
      return HttpResponse.json({
        job_id: job.id,
        status: "running",
        capability: job.capability,
      });
    }),
  ];
}
