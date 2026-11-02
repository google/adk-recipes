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

// Generations as jobs (studio/jobs.py), as the MCP App panel follows them: start one, poll its
// status with backoff showing each variation as it is judged, and when it ends send
// `job_finished` so the server records its results in the studio. One that ends in error
// can be started again with the same spec ("Try again"), which also recomputes what the
// server's preprocessing cache kept for it (`refresh_cache`).
import {
  getStudioJob,
  startStudioJob,
  type JobSpec,
  type JobStatus,
  type StudioCapability,
} from "../../api/studio";
import { errorMessage } from "../../shared/files";
import { jobSpec, missingInput, resultsOf, type UrlCache } from "./model";
import type { LiveJob, StudioStore } from "./store";

/** Poll timing (tests shorten it). */
export const POLLING = {
  intervalMs: 4000,
  /** Waits after a failed status poll; then the following stops. */
  retryMs: [4000, 8000, 16000, 30000, 30000],
  limitMs: 30 * 60 * 1000,
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** A job's results so far, and its try-on images. */
function liveOf(prev: LiveJob, st: JobStatus, urls: UrlCache): LiveJob {
  const images = resultsOf(st.images, urls);
  const tryonImage =
    typeof st.tryon_image === "string" && st.tryon_image
      ? st.tryon_image
      : prev.tryonImage;
  return {
    cap: prev.cap,
    results: resultsOf(st.results, urls),
    images,
    tryonImage,
  };
}

export class JobFollower {
  /** Id of the generation being followed. */
  polling: string | null = null;
  private run = 0;
  /** Generations already followed in this page load. */
  private readonly followed = new Set<string>();
  private readonly store: StudioStore;

  constructor(store: StudioStore) {
    this.store = store;
  }

  stop(): void {
    this.run++;
    this.polling = null;
    this.followed.clear();
  }

  /** Follow the studio's running generation, e.g. one the agent started or a reopened studio's. */
  follow(): void {
    const { job } = this.store.state.view;
    if (job && !this.polling && !this.followed.has(job.id))
      void this.poll(job.id, job.capability, null, job.tryonImage);
  }

  /** Start a full generation of the current page (once: while it starts, Generate does nothing). */
  generate = async () => {
    if (this.busy) return;
    const { view } = this.store.state;
    const missing = missingInput(view, view.capability);
    this.store.set({ jobError: missing, retry: null });
    if (!missing) await this.start(jobSpec(view, view.capability));
  };

  /** "Make video": remake only the videos of one of the studio's Video Try-On images (its `uri`). */
  makeVideo = (uri: string) =>
    this.start(jobSpec(this.store.state.view, "video_tryon", uri));

  /** "Try again": the spec of the last generation, which ended in error, bypassing the server's preprocessing cache. */
  tryAgain = async () => {
    const { retry } = this.store.state;
    if (retry) await this.start({ ...retry, refresh_cache: true });
  };

  /** A generation is starting or running, or the saved studio is not read yet: nothing starts. */
  private get busy(): boolean {
    const { ready, view, starting } = this.store.state;
    return !ready || !!view.job || !!this.polling || starting;
  }

  private async start(spec: JobSpec): Promise<void> {
    if (this.busy) return;
    const { epoch } = this.store;
    this.store.set({ starting: true, jobError: "", retry: null });
    try {
      const start = await startStudioJob(this.store.state.studioId, spec);
      if (epoch !== this.store.epoch) return; // "New" meanwhile: the job stays with the studio it started in
      if (typeof start.job_id !== "string" || !start.job_id)
        throw new Error("The generation did not start.");
      void this.poll(start.job_id, spec.capability, spec);
      void this.store.act({
        job_started: { job_id: start.job_id, capability: spec.capability },
      }); // saved with the studio
    } catch (e) {
      if (epoch === this.store.epoch)
        this.store.set({ jobError: errorMessage(e), retry: spec });
    } finally {
      if (epoch === this.store.epoch) this.store.set({ starting: false });
    }
  }

  /**
   * Follow a generation (`spec`: just started here with it, else null; `tryonImage`: the image of a "Make video",
   * "" for a full run). When it ends the server records its results (job_finished) and the page shows that state;
   * when the following stops before the end, the server's state is shown again (which may follow the job anew).
   */
  private async poll(
    jobId: string,
    cap: StudioCapability,
    spec: JobSpec | null,
    tryonImage = spec?.tryon_image ?? "",
  ): Promise<void> {
    if (this.polling) return;
    this.polling = jobId;
    this.followed.add(jobId);
    let live: LiveJob = { cap, results: [], images: [], tryonImage };
    this.store.set({
      following: true,
      live,
      jobStatus: "starting…",
      jobError: "",
      retry: null,
    });
    const run = ++this.run;
    const startedAt = Date.now();
    let wait = spec ? POLLING.intervalMs : 0;
    let ended = false;
    let failures = 0;
    try {
      for (;;) {
        await sleep(wait);
        wait = POLLING.intervalMs;
        if (run !== this.run) return;
        if (Date.now() - startedAt > POLLING.limitMs) {
          this.store.set({
            jobError: "Stopped following the generation after 30 minutes.",
          });
          break;
        }
        let st: JobStatus;
        try {
          st = await getStudioJob(jobId);
          failures = 0;
        } catch (e) {
          // A timeout or a hiccup: try again, later each time.
          if (run !== this.run) return;
          if (failures >= POLLING.retryMs.length) throw e;
          wait = POLLING.retryMs[failures++];
          this.store.set({ jobStatus: "reconnecting…" });
          continue;
        }
        if (run !== this.run) return;
        if (st.status === "unknown") {
          // No longer kept.
          ended = true;
          break;
        }
        // Each variation shows as soon as it is judged; the final order is the judge's.
        live = liveOf(live, st, this.store.urls);
        this.store.set({
          jobStatus: [st.progress ?? "", `${Number(st.elapsed_s) || 0} s`]
            .filter(Boolean)
            .join(" · "),
          live,
        });
        if (st.status === "running") continue;
        // done, or error (e.g. "interrupted: ..." when its server stopped): the same spec can run again.
        if (st.status !== "done") {
          const retry =
            spec ?? jobSpec(this.store.state.view, cap, live.tryonImage);
          this.store.set({
            jobError: st.error || `Generation ${st.status || "failed"}`,
            retry,
          });
        }
        ended = true;
        break;
      }
    } catch (e) {
      if (run !== this.run) return;
      this.store.set({ jobError: errorMessage(e) });
    }
    this.polling = null;
    this.store.set({ following: false });
    if (ended) {
      await this.store.act({ job_finished: { job_id: jobId } }); // its results, saved with the studio
      return;
    }
    this.followed.delete(jobId);
    await this.store.refresh();
  }
}
