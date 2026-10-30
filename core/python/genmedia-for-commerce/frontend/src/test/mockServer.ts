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

// Fixture backend for the web app (the Studio API in ./studioMock, plus the media,
// catalogue image and status routes it relies on), shared by the tests (msw/node) and
// `npm run dev:mock` (Vite middleware). Relative imports only: the Vite config
// loader does not resolve the `@/` alias.
import { http, HttpResponse } from "msw";
import { studioHandlers } from "./studioMock";

const PNGS = [
  "iVBORw0KGgoAAAANSUhEUgAAAAMAAAAECAIAAADETxJQAAAAEElEQVR42mPo2vIDghjwsgANvhqJRAIRNgAAAABJRU5ErkJggg==",
  "iVBORw0KGgoAAAANSUhEUgAAAAMAAAAECAIAAADETxJQAAAAEElEQVR42mMwuTwTghjwsgCBexOB066GCAAAAABJRU5ErkJggg==",
  "iVBORw0KGgoAAAANSUhEUgAAAAMAAAAECAIAAADETxJQAAAAEElEQVR42mP4UVgIQQx4WQDC6xY5ly7EOgAAAABJRU5ErkJggg==",
];
const options = { eventDelayMs: 0, video: "AAAAGGZ0eXBtcDQyAAAAAG1wNDJpc29t" };

/** dev:mock paces the jobs (and could swap in a playable base64 mp4). */
export function configureMock(overrides: Partial<typeof options>): void {
  Object.assign(options, overrides);
}

const MEDIA_BUCKET = "demo-genmedia-for-commerce-media-payloads";

const badRequest = (detail: string) =>
  HttpResponse.json({ detail }, { status: 400 });
const bytes = (b64: string, type: string) =>
  new HttpResponse(
    Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)),
    { headers: { "Content-Type": type } },
  );
const png = (index = 0) => bytes(PNGS[index % PNGS.length], "image/png");

export const handlers = [
  http.get("/api/media", ({ request }) => {
    const uri = new URL(request.url).searchParams.get("uri") ?? "";
    if (!uri.startsWith(`gs://${MEDIA_BUCKET}/runs/`) || uri.includes(".."))
      return badRequest("Invalid media uri");
    return uri.endsWith(".mp4")
      ? bytes(options.video, "video/mp4")
      : png(Number(uri.match(/(\d+)\.(?:png|webp)$/)?.[1] ?? 0));
  }),
  http.get("/api/catalog/image/:id/:n", ({ params }) =>
    png(String(params.id).length + Number(params.n)),
  ),
  http.get("/health", () => HttpResponse.json({ status: "healthy" })),
  // A studio job adds a variation every 4 event delays (none in the tests).
  ...studioHandlers({
    bucket: MEDIA_BUCKET,
    delayMs: () => options.eventDelayMs * 4,
  }),
];
