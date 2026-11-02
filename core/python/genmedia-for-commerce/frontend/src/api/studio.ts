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

// The Studio REST API of the web app (/api/studio/*): the server owns the studio
// state; the client renders it, sends each click as an action and follows jobs.
import {
  getJson,
  postForm,
  postJson,
  toFormData,
  type RequestOptions,
} from "./client";

export type StudioCapability = "tryon" | "video_tryon" | "spin";

/** An image of the studio: a `catalog:` or `gs://` ref with a label (thumb: a browser URL, client side only). */
export type StudioItem = { label: string; ref: string; thumb?: string };

/**
 * One generated variation (`url`: its browser preview, absent in saved states until renewed), or a try-on image a
 * Video Try-On animated (`passed: false`: the best one, animated although none passed its checks).
 */
export type StudioResult = {
  index?: number;
  status?: string;
  score?: number | null;
  uri?: string;
  url?: string;
  note?: string;
  view?: string;
  /** A video's try-on image: the `uri` of an item of the studio's `images.video_tryon`. */
  source?: string;
  passed?: boolean;
};

/** A studio state as the server stores it (studio/patches.py); read defensively by `fromServer`. */
export type StudioStateDoc = Record<string, unknown>;

/** One studio action (an op of studio/patches.py, or `{job_finished: {job_id}}`). */
export type StudioAction = Record<string, unknown>;

export type SavedStudio = {
  studio_id: string;
  saved: boolean;
  updated_at: number;
  state: StudioStateDoc;
};
export type ActionResult = {
  state: StudioStateDoc;
  updated_at: number;
  refs?: Record<string, string>;
};
export type AgentTurn = {
  studio_id: string;
  reply: string;
  patches: unknown[];
  issued_at: number;
  /** The studio the turn saved, when it saved one. */
  state?: StudioStateDoc;
  updated_at?: number;
};
export type ChatMessage = { role: "user" | "agent"; text: string };
export type StudioUpload = {
  ref: string;
  label: string;
  thumb: string;
  description: string;
};
export type StudioCatalogResults = {
  query: string;
  items: StudioItem[];
  error: string | null;
};
/** A spin set of the catalogue: one ref and one thumbnail per view (`thumb`: the first view's). */
export type GalleryProduct = {
  label: string;
  refs: string[];
  thumb: string;
  thumbs?: string[];
};
/** The catalogue's preset models and spin sets (products: `searchStudioCatalog`). */
export type StudioGallery = { models: StudioItem[]; spin: GalleryProduct[] };

/** A generation request (the studio id comes from the path). */
export type JobSpec = {
  capability: StudioCapability;
  person: string;
  reference_model: string;
  products: string[];
  /** Video Try-On "Make video": the try-on image (of the studio's `images.video_tryon`) to remake the videos of; "" for a full run. */
  tryon_image: string;
  spin: string[];
  settings: Record<string, string | number | boolean>;
  /** "Try again": recompute what the server's preprocessing cache keeps instead of reading it. */
  refresh_cache?: boolean;
};
export type JobStarted = {
  job_id: string;
  status: string;
  capability: StudioCapability;
};
export type JobStatus = {
  job_id: string;
  status: "running" | "done" | "error" | "unknown";
  progress?: string;
  elapsed_s?: number;
  results?: StudioResult[];
  /** Video Try-On: the try-on images animated so far. */
  images?: StudioResult[];
  /** The try-on image of a "Make video" job ("" or absent for a full run). */
  tryon_image?: string;
  error?: string;
};

const base = (studioId: string) =>
  `/api/studio/${encodeURIComponent(studioId)}`;

export function agentTurn(
  studioId: string,
  message: string,
  options: RequestOptions = {},
): Promise<AgentTurn> {
  return postJson(`${base(studioId)}/agent`, { message }, options);
}

export function getStudioState(
  studioId: string,
  options: RequestOptions = {},
): Promise<SavedStudio> {
  return getJson(`${base(studioId)}/state`, options);
}

export function getStudioMessages(
  studioId: string,
  options: RequestOptions = {},
): Promise<{ studio_id: string; messages: ChatMessage[] }> {
  return getJson(`${base(studioId)}/messages`, options);
}

export function sendStudioAction(
  studioId: string,
  event: StudioAction,
  options: RequestOptions = {},
): Promise<ActionResult> {
  return postJson(`${base(studioId)}/actions`, { event }, options);
}

export async function uploadStudioImage(
  studioId: string,
  file: Blob,
  options: RequestOptions = {},
): Promise<StudioUpload> {
  return (
    await postForm(`${base(studioId)}/uploads`, toFormData({ file }), options)
  ).json();
}

/** The products picker: the featured products for an empty query, else the best matches. */
export function searchStudioCatalog(
  studioId: string,
  query: string,
  audience = "",
  options: RequestOptions = {},
): Promise<StudioCatalogResults> {
  return postJson(
    `${base(studioId)}/catalog-search`,
    { query, audience },
    options,
  );
}

export function startStudioJob(
  studioId: string,
  spec: JobSpec,
  options: RequestOptions = {},
): Promise<JobStarted> {
  return postJson(`${base(studioId)}/jobs`, spec, options);
}

export function getStudioJob(
  jobId: string,
  options: RequestOptions = {},
): Promise<JobStatus> {
  return getJson(`/api/studio/jobs/${encodeURIComponent(jobId)}`, options);
}

export function getStudioGallery(
  options: RequestOptions = {},
): Promise<StudioGallery> {
  return getJson("/api/studio/gallery", options);
}

export function getStudioPreviews(
  refs: string[],
  options: RequestOptions = {},
): Promise<{ previews: Record<string, string> }> {
  return postJson("/api/studio/previews", { refs }, options);
}
