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

import { useState } from "react";
import type { StudioResult } from "../../api/studio";
import { SpinnerIcon, StarIcon, VideoIcon } from "../../components/icons";
import ImageLightbox from "../../components/ImageLightbox";
import { EmptyResults } from "../../components/RunControls";
import type { LiveJob } from "./jobs";
import { CAPS, safeUrl } from "./model";
import { FailedNote, PendingCard } from "./Pending";
import ScoreBadge from "./ScoreBadge";
import TryonImages from "./TryonImages";
import type { Studio } from "./useStudio";

const pill = "chip backdrop-blur-md shadow-level-2";

/** What each page needs before its first generation. */
const STEPS = {
  tryon: ["Choose a person", "Add products", "Generate"],
  video_tryon: ["Add a person and products", "Pick the motion", "Generate"],
  spin: ["Add product photos or a spin set", "Generate"],
};
/** What one result of each page is. */
const NOUNS = { tryon: "image", video_tryon: "video", spin: "spin" };

/** The extension of a result's file: `mp4` a video (Video Try-On), anything else an image (a spin is an animated WebP). */
const extOf = (r: StudioResult) =>
  /\.(\w+)$/.exec(r.uri ?? "")?.[1].toLowerCase() ?? "png";

/** "3:4" → "3 / 4" for CSS; "auto" falls back to portrait. */
const cssRatio = (ratio: unknown) =>
  typeof ratio === "string" && ratio.includes(":")
    ? ratio.replace(":", " / ")
    : "3 / 4";

type CardProps = {
  result: StudioResult;
  name: string;
  best: boolean;
  ratio: string;
  /** "From image N": the try-on image a video was made from. */
  from?: string;
  /** Its try-on image is hovered or focused. */
  linked?: boolean;
  onLink?: (uri: string) => void;
  /** "Animate" (a Photo Try-On front view): its videos on Video Try-On; `canAnimate`: no generation runs. */
  onAnimate?: () => void;
  canAnimate?: boolean;
};

function ResultCard({
  result,
  name,
  best,
  ratio,
  from,
  linked,
  onLink,
  onAnimate,
  canAnimate,
}: CardProps) {
  const [zoomed, setZoomed] = useState(false);
  const ext = extOf(result);
  const video = ext === "mp4";
  const url = safeUrl(result.url);
  // A spin's MP4: the video model's raw clip (not cut to the loop, not reversed); the WebP is the result shown.
  const videoUrl = safeUrl(result.video_url);
  const rejected = result.status === "rejected";
  // Made with its checks off: no score, never called publishable.
  const checked = typeof result.score === "number";
  const verdict = rejected
    ? "Not publishable"
    : checked
      ? "Publishable"
      : "Not checked";
  const link = (on: boolean) => onLink?.(on ? (result.source ?? "") : "");
  const ring = linked
    ? "ring-2 ring-gm-accent/60 shadow-accent-glow"
    : best
      ? "ring-1 ring-gm-accent/40 shadow-accent-glow"
      : "";
  return (
    <article
      onMouseEnter={() => link(true)}
      onMouseLeave={() => link(false)}
      onFocus={() => link(true)}
      onBlur={() => link(false)}
      className={`glass-panel rounded-2xl overflow-hidden flex flex-col animate-fade-in-up transition-shadow ${ring}`}
    >
      <div className="relative bg-black/40" style={{ aspectRatio: ratio }}>
        {url && video && (
          <video
            src={url}
            aria-label={`${name} video`}
            controls
            loop
            muted
            playsInline
            className="w-full h-full object-contain"
          />
        )}
        {url && !video && (
          <button
            type="button"
            onClick={() => setZoomed(true)}
            className="w-full h-full"
            aria-label={`Enlarge ${name}`}
          >
            <img
              src={url}
              alt={name}
              className="w-full h-full object-contain"
            />
          </button>
        )}
        {!url && (
          <p className="absolute inset-0 flex items-center justify-center p-4 text-sm text-gm-text-tertiary">
            Preview unavailable
          </p>
        )}
        <div className="absolute top-2.5 inset-x-2.5 flex justify-between gap-2 pointer-events-none">
          {best ? (
            <span className={`${pill} bg-gm-accent text-gm-bg font-semibold`}>
              <StarIcon size={11} />
              Best
            </span>
          ) : (
            <span />
          )}
          {result.view === "back" && (
            <span
              className={`${pill} bg-black/60 text-white ring-1 ring-white/20`}
            >
              Back
            </span>
          )}
        </div>
      </div>
      <div className="p-4 flex flex-col gap-2.5 text-sm flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium whitespace-nowrap">{name}</span>
          {typeof result.score === "number" && (
            <ScoreBadge score={result.score} />
          )}
        </div>
        {from && (
          <span className="chip self-start bg-white/[0.06] text-gm-text-secondary">
            {from}
          </span>
        )}
        <p
          className={`flex items-center gap-1.5 text-caption ${rejected ? "text-gm-warning" : "text-gm-text-secondary"}`}
        >
          <span
            aria-hidden="true"
            className={`w-1.5 h-1.5 rounded-full ${rejected ? "bg-gm-warning" : "bg-gm-success"}`}
          />
          {verdict}
        </p>
        {result.note && result.note !== verdict && (
          <p className="text-xs leading-relaxed text-gm-text-secondary">
            {result.note}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2 mt-auto pt-1">
          {url && (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="glass-button rounded-lg h-8 px-3 text-xs font-medium flex items-center"
            >
              Open
            </a>
          )}
          {videoUrl && (
            <a
              href={videoUrl}
              download
              target="_blank"
              rel="noopener noreferrer"
              className="glass-button rounded-lg h-8 px-3 text-xs font-medium flex items-center whitespace-nowrap"
            >
              Download video
            </a>
          )}
          {onAnimate && (
            <button
              type="button"
              onClick={onAnimate}
              disabled={!canAnimate}
              aria-label={`Animate ${name}`}
              title="Make videos of this image on Video Try-On"
              className="glass-button rounded-lg h-8 px-3 text-xs font-medium flex items-center gap-1.5 whitespace-nowrap"
            >
              <VideoIcon size={14} className="text-gm-accent" />
              Animate
            </button>
          )}
        </div>
      </div>
      {zoomed && url && (
        <ImageLightbox src={url} alt={name} onClose={() => setZoomed(false)} />
      )}
    </article>
  );
}

/**
 * The saved videos with those of a running "Make video" job instead of its image's, as the server saves them:
 * last, numbered after the highest saved index.
 */
function withRemade(saved: StudioResult[], live: LiveJob): StudioResult[] {
  const top = Math.max(
    -1,
    ...saved.map((r) => (typeof r.index === "number" ? r.index : -1)),
  );
  const made = live.results.map((r, n) => ({
    ...r,
    index: top + 1 + n,
    source: r.source || live.tryonImage,
  }));
  return [...saved.filter((r) => r.source !== live.tryonImage), ...made];
}

/**
 * The videos were all ranked together, by one job: each job writes to its own run folder
 * (`runs/<date>/studio-<id>/<run>/outputs/`), so after a "Make video" the page holds two.
 */
const oneJob = (results: StudioResult[]) =>
  new Set(results.map((r) => (r.uri ?? "").replace(/[^/]*$/, ""))).size <= 1;

/** The page's results: the generation being followed so far, else the studio's; Video Try-On's try-on images first. */
export default function StudioResults({ studio }: { studio: Studio }) {
  const [linked, setLinked] = useState("");
  const cap = studio.view.capability;
  const live = studio.live?.cap === cap ? studio.live : null;
  const running = !!live && studio.following;
  const status = studio.jobStatus || "Generating";
  const remake = live?.tryonImage ?? "";
  const saved = studio.view.results[cap];
  const results = !live
    ? saved
    : remake
      ? withRemade(saved, live)
      : live.results;
  const images =
    cap !== "video_tryon"
      ? []
      : live && !remake
        ? live.images
        : studio.view.images;
  const ratio = cssRatio(studio.view.settings[cap].aspect_ratio);
  // Shown while the job runs: a card per variation still coming.
  const coming = running ? live.coming : { image_results: 0, results: 0 };
  const failed = live?.failed ?? { image_results: 0, results: 0 };
  const canMake =
    studio.ready && !studio.view.job && !studio.following && !studio.starting;
  const imageOf = (r: StudioResult) =>
    r.source ? images.findIndex((img) => img.uri === r.source) : -1;
  // The judge's best is first; once a "Make video" adds videos it never compared, no video is the best.
  const ranked = !running && (cap !== "video_tryon" || oneJob(results));

  if (!results.length && !images.length && !running) {
    return (
      <EmptyResults
        title={`Your ${CAPS[cap]} results appear here`}
        text="Ask the studio agent, or set the inputs and press Generate."
      >
        <ol className="flex flex-wrap justify-center gap-2">
          {STEPS[cap].map((step, i) => (
            <li
              key={step}
              className="chip bg-white/[0.05] ring-1 ring-inset ring-white/10 text-gm-text-secondary !text-xs !py-1 !px-2.5"
            >
              <span className="text-gm-accent font-semibold">{i + 1}</span>{" "}
              {step}
            </li>
          ))}
        </ol>
      </EmptyResults>
    );
  }
  return (
    <section className="flex flex-col gap-4">
      {images.length + coming.image_results + failed.image_results > 0 && (
        <TryonImages
          images={images}
          coming={coming.image_results}
          failed={failed.image_results}
          videos={results}
          making={running ? remake : ""}
          canMake={canMake}
          linked={linked}
          onLink={setLinked}
          onMake={(uri) => void studio.makeVideo(uri)}
          ratio={ratio}
        />
      )}
      <div className="flex items-center gap-2 min-h-[32px] min-w-0">
        <h2 className="text-h2 whitespace-nowrap">
          {cap === "video_tryon" ? "Videos" : "Results"}
          {running ? " so far" : ""}
        </h2>
        <span className="chip bg-white/[0.06] text-gm-text-secondary">
          {results.length}
        </span>
        <FailedNote count={failed.results} noun={NOUNS[cap]} />
        {running && (
          // One line, cut to fit: the whole status is under the Generate button.
          <span
            title={status}
            className="ml-auto min-w-0 chip bg-gm-accent/10 text-gm-accent ring-1 ring-inset ring-gm-accent/25 !py-1"
          >
            <SpinnerIcon size={12} className="shrink-0" />
            <span className="truncate">{status}</span>
          </span>
        )}
      </div>
      {results.length + coming.results > 0 && (
        <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 2xl:grid-cols-3">
          {results.map((r, i) => {
            const n = imageOf(r);
            return (
              <ResultCard
                // By uri: a card (a playing video) stays when the results are ranked or more arrive.
                key={r.uri || `#${i}`}
                result={r}
                name={`Variation ${(r.index ?? i) + 1}`}
                ratio={ratio}
                best={i === 0 && ranked && typeof r.score === "number"}
                from={n >= 0 ? `From image ${n + 1}` : undefined}
                linked={n >= 0 && linked === r.source}
                onLink={n >= 0 ? setLinked : undefined}
                onAnimate={
                  cap === "tryon" && r.view !== "back" && r.uri
                    ? () => void studio.animate(r.uri ?? "")
                    : undefined
                }
                canAnimate={canMake}
              />
            );
          })}
          {Array.from({ length: coming.results }, (_, n) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: placeholders are all alike
            <PendingCard key={`pending-${n}`} ratio={ratio} />
          ))}
        </div>
      )}
    </section>
  );
}
