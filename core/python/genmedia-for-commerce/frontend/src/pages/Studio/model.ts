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

// The studio as the page renders it, built from the server's state (studio/patches.py),
// as the MCP App panel does (mcp/static/studio.html): known keys and plain values only.
import type {
  JobSpec,
  StudioCapability,
  StudioItem,
  StudioResult,
  StudioStateDoc,
} from "../../api/studio";

export type Setting = string | number | boolean;
export type Settings = Record<string, Setting>;
/** The generation the studio follows; `tryonImage`: the image of a "Make video" job ("" for a full run). */
export type StudioJob = {
  id: string;
  capability: StudioCapability;
  tryonImage: string;
};

export type StudioView = {
  capability: StudioCapability;
  person: StudioItem | null;
  reference_model: StudioItem | null;
  products: StudioItem[];
  spin: StudioItem[];
  settings: Record<StudioCapability, Settings>;
  results: Record<StudioCapability, StudioResult[]>;
  /** The try-on images the last full Video Try-On animated, in display order ("Image 1", "Image 2"...). */
  images: StudioResult[];
  job: StudioJob | null;
};

/** Browser URLs by image ref or result uri: what the server's state does not carry. */
export type UrlCache = Map<string, string>;

export const CAPS: Record<StudioCapability, string> = {
  tryon: "Photo Try-On",
  video_tryon: "Video Try-On",
  spin: "360° Spin",
};
export const CAP_KEYS = Object.keys(CAPS) as StudioCapability[];
export const MAX_ITEMS = 8;

/** Shown until the studio sets them. */
const DEFAULTS: Record<StudioCapability, Settings> = {
  tryon: {
    scenario: "",
    style_guidelines: "",
    aspect_ratio: "3:4",
    image_size: "2K",
    num_variations: "1",
    complete_look: true,
    upscale_crops: false,
  },
  video_tryon: {
    motion: "auto",
    prompt: "",
    aspect_ratio: "9:16",
    resolution: "720p",
    num_variations: "1",
  },
  spin: { aspect_ratio: "auto", resolution: "720p", num_variations: "1" },
};

type SelectSetting = { key: string; label: string; options: string[] };
const VARIATIONS: SelectSetting = {
  key: "num_variations",
  label: "Variations",
  options: ["1", "2", "3"],
};
const RESOLUTION: SelectSetting = {
  key: "resolution",
  label: "Resolution",
  options: ["720p", "1080p"],
};

/** What Variations counts on the current page: Video Try-On makes try-on images (each animated into the
 * deployment's videos per image, 2 by default). */
export function variationsOf(v: StudioView): { label: string; hint: string } {
  return v.capability === "video_tryon"
    ? {
        label: "Try-on images",
        hint: "Try-on images to make. Each one animated gets 2 videos (unless the deployment sets another number).",
      }
    : { label: "Variations", hint: "" };
}

/** The run options of each page and their values. */
export const RUN_OPTIONS: Record<StudioCapability, SelectSetting[]> = {
  tryon: [
    VARIATIONS,
    {
      key: "aspect_ratio",
      label: "Aspect ratio",
      options: ["3:4", "1:1", "4:3", "9:16", "16:9"],
    },
    { key: "image_size", label: "Image size", options: ["1K", "2K", "4K"] },
  ],
  video_tryon: [
    VARIATIONS,
    { key: "aspect_ratio", label: "Aspect ratio", options: ["9:16", "16:9"] },
    RESOLUTION,
  ],
  spin: [
    VARIATIONS,
    {
      key: "aspect_ratio",
      label: "Aspect ratio",
      options: ["auto", "16:9", "9:16"],
    },
    RESOLUTION,
  ],
};

export function blank(): StudioView {
  return {
    capability: "tryon",
    person: null,
    reference_model: null,
    products: [],
    spin: [],
    settings: {
      tryon: { ...DEFAULTS.tryon },
      video_tryon: { ...DEFAULTS.video_tryon },
      spin: { ...DEFAULTS.spin },
    },
    results: { tryon: [], video_tryon: [], spin: [] },
    images: [],
    job: null,
  };
}

const ITEM_KEYS = ["label", "ref", "thumb"] as const;
const RESULT_KEYS = [
  "index",
  "status",
  "score",
  "uri",
  "url",
  "view",
  "note",
  "source",
  "passed",
] as const;

const isObj = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const isPrim = (v: unknown): v is Setting =>
  typeof v === "string" || typeof v === "number" || typeof v === "boolean";
const arr = (x: unknown): unknown[] => (Array.isArray(x) ? x : []);
const own = (o: Record<string, unknown>, k: string) =>
  // biome-ignore lint/suspicious/noPrototypeBuiltins: Object.hasOwn is ES2022; the app targets ES2020
  Object.prototype.hasOwnProperty.call(o, k) ? o[k] : undefined;
export const capOf = (c: unknown): StudioCapability | null =>
  CAP_KEYS.find((k) => k === c) ?? null;

/** Browser URLs the page may load: https, same-origin paths, or inline images and videos. */
export function safeUrl(u: unknown): string {
  if (typeof u !== "string") return "";
  if (/^data:(image|video)\/[a-z0-9.+-]+[;,]/i.test(u)) return u;
  if (u.startsWith("/") && !u.startsWith("//")) return u;
  try {
    return new URL(u).protocol === "https:" ? u : "";
  } catch {
    return "";
  }
}

export function cleanItem(x: unknown): StudioItem | null {
  if (!isObj(x) || typeof x.ref !== "string") return null;
  const item: Record<string, string> = {};
  for (const k of ITEM_KEYS)
    if (typeof own(x, k) === "string") item[k] = x[k] as string;
  return item as StudioItem;
}

/** `{label, ref}`: what an action carries (the server finds no use for thumbnails). */
export const lean = (item: StudioItem) => ({
  label: item.label,
  ref: item.ref,
});

function withThumb(x: unknown, cache: UrlCache): StudioItem | null {
  const item = cleanItem(x);
  if (item) item.thumb = safeUrl(item.thumb) || cache.get(item.ref) || "";
  return item;
}

/** Results with their preview URLs: the server's (remembered in `cache`), else the ones already seen. */
export function resultsOf(list: unknown, cache: UrlCache): StudioResult[] {
  return arr(list)
    .filter(isObj)
    .map((r) => {
      const out: Record<string, unknown> = {};
      for (const k of RESULT_KEYS) {
        const v = own(r, k);
        if (isPrim(v) || v === null) out[k] = v;
      }
      const res = out as StudioResult;
      if (typeof res.uri === "string" && typeof res.url === "string" && res.url)
        cache.set(res.uri, res.url);
      else if (typeof res.uri === "string") res.url = cache.get(res.uri) ?? "";
      return res;
    });
}

/** A state from the server, ready to render. */
export function fromServer(st: StudioStateDoc, cache: UrlCache): StudioView {
  const s = blank();
  s.capability = capOf(st.capability) ?? s.capability;
  s.person = withThumb(st.person, cache);
  s.reference_model = withThumb(st.reference_model, cache);
  s.products = arr(st.products)
    .map((x) => withThumb(x, cache))
    .filter((x): x is StudioItem => !!x);
  s.spin = arr(st.spin)
    .map((x) => withThumb(x, cache))
    .filter((x): x is StudioItem => !!x);
  const settings = isObj(st.settings) ? st.settings : {};
  const results = isObj(st.results) ? st.results : {};
  for (const c of CAP_KEYS) {
    const page = isObj(own(settings, c))
      ? (settings[c] as Record<string, unknown>)
      : {};
    for (const k of Object.keys(DEFAULTS[c])) {
      const v = own(page, k);
      if (isPrim(v)) s.settings[c][k] = v;
    }
    // Kept without a usable url too (its preview could not be renewed).
    s.results[c] = resultsOf(own(results, c), cache).filter(
      (r) => r.uri || r.url,
    );
  }
  const images = isObj(st.images) ? own(st.images, "video_tryon") : undefined;
  s.images = resultsOf(images, cache).filter((r) => r.uri || r.url);
  // Studios saved with the old catwalk / turnaround motions.
  if (s.settings.video_tryon.motion !== "custom")
    s.settings.video_tryon.motion = "auto";
  const job = isObj(st.job) ? st.job : null;
  const jobCap = job && capOf(job.capability);
  if (job && typeof job.job_id === "string" && job.job_id && jobCap) {
    const image = own(job, "tryon_image");
    s.job = {
      id: job.job_id,
      capability: jobCap,
      tryonImage:
        jobCap === "video_tryon" && typeof image === "string" ? image : "",
    };
  }
  return s;
}

const allItems = (v: StudioView) =>
  [v.person, v.reference_model, ...v.products, ...v.spin].filter(
    (x): x is StudioItem => !!x,
  );

/** Refs of the studio's images that have no thumbnail. */
export const refsWithoutThumb = (v: StudioView) => [
  ...new Set(
    allItems(v)
      .filter((i) => !i.thumb)
      .map((i) => i.ref),
  ),
];

/** The view with the thumbnails `cache` now has for its images. */
export function withThumbs(v: StudioView, cache: UrlCache): StudioView {
  const fill = (i: StudioItem | null) =>
    i && !i.thumb && cache.has(i.ref) ? { ...i, thumb: cache.get(i.ref) } : i;
  return {
    ...v,
    person: fill(v.person),
    reference_model: fill(v.reference_model),
    products: v.products.map((i) => fill(i) as StudioItem),
    spin: v.spin.map((i) => fill(i) as StudioItem),
  };
}

/** The generation request for page `cap`; `tryonImage` (Video Try-On): remake only that try-on image's videos ("Make video"). */
export function jobSpec(
  v: StudioView,
  cap: StudioCapability,
  tryonImage = "",
): JobSpec {
  const refs = (xs: StudioItem[]) => xs.map((x) => x.ref);
  return {
    capability: cap,
    person: v.person?.ref ?? "",
    reference_model: v.reference_model?.ref ?? "",
    products: refs(v.products),
    tryon_image: cap === "video_tryon" ? tryonImage : "",
    spin: refs(v.spin),
    settings: { ...v.settings.tryon, ...v.settings[cap] },
  };
}

/** What page `cap` still needs before it can generate ("" when ready). */
export function missingInput(v: StudioView, cap: StudioCapability): string {
  if (cap === "spin")
    return v.spin.length ? "" : "Add product photos to spin first.";
  if (!v.person) return "Choose a person first.";
  if (!v.products.length) return "Add at least one product first.";
  return "";
}
