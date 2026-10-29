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

import { useState } from "react";
import type { StudioResult } from "../../api/studio";
import { DownloadIcon, SpinnerIcon, StarIcon } from "../../components/icons";
import ImageLightbox from "../../components/ImageLightbox";
import { EmptyResults } from "../../components/RunControls";
import { CAPS, safeUrl } from "./model";
import ScoreBadge from "./ScoreBadge";
import type { LiveJob } from "./store";
import TryonImages from "./TryonImages";
import type { Studio } from "./useStudio";

const pill = "chip backdrop-blur-md shadow-level-2";

/** What each page needs before its first generation. */
const STEPS = {
  tryon: ["Choose a person", "Add products", "Generate"],
  video_tryon: ["Add a person and products", "Pick the motion", "Generate"],
  spin: ["Add product photos or a spin set", "Generate"],
};

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
};

function ResultCard({
  result,
  name,
  best,
  ratio,
  from,
  linked,
  onLink,
}: CardProps) {
  const [zoomed, setZoomed] = useState(false);
  const ext = extOf(result);
  const video = ext === "mp4";
  const url = safeUrl(result.url);
  const rejected = result.status === "rejected";
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
          {rejected ? "Not publishable" : "Publishable"}
        </p>
        {result.note && (
          <p className="text-xs leading-relaxed text-gm-text-secondary">
            {result.note}
          </p>
        )}
        <div className="flex items-center gap-2 mt-auto pt-1">
          {url && (
            <a
              href={url}
              download={`${name.toLowerCase().replace(/\s+/g, "-")}.${ext}`}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Download ${name}`}
              title="Download"
              className="glass-button rounded-lg w-8 h-8 flex items-center justify-center"
            >
              <DownloadIcon size={15} />
            </a>
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
      {images.length > 0 && (
        <TryonImages
          images={images}
          videos={results}
          making={running ? remake : ""}
          canMake={canMake}
          linked={linked}
          onLink={setLinked}
          onMake={(uri) => void studio.makeVideo(uri)}
          ratio={ratio}
        />
      )}
      <div className="flex items-center gap-2 min-h-[32px]">
        <h2 className="text-h2">
          {cap === "video_tryon" ? "Videos" : "Results"}
          {running ? " so far" : ""}
        </h2>
        <span className="chip bg-white/[0.06] text-gm-text-secondary">
          {results.length}
        </span>
        {running && (
          <span className="ml-auto chip bg-gm-accent/10 text-gm-accent ring-1 ring-inset ring-gm-accent/25 !py-1">
            <SpinnerIcon size={12} /> {studio.jobStatus || "Generating"}
          </span>
        )}
      </div>
      {results.length > 0 && (
        <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 2xl:grid-cols-3">
          {results.map((r, i) => {
            const n = imageOf(r);
            return (
              <ResultCard
                // biome-ignore lint/suspicious/noArrayIndexKey: a result can have no uri yet
                key={`${r.uri ?? ""}-${i}`}
                result={r}
                name={`Variation ${(r.index ?? i) + 1}`}
                ratio={ratio}
                best={i === 0 && ranked}
                from={n >= 0 ? `From image ${n + 1}` : undefined}
                linked={n >= 0 && linked === r.source}
                onLink={n >= 0 ? setLinked : undefined}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}
