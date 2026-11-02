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

// Video Try-On's try-on images: the ones its last full run animated, each with its videos'
// count and "Make video", which remakes only that image's videos.
import { useState } from "react";
import type { StudioResult } from "../../api/studio";
import { SpinnerIcon, VideoIcon } from "../../components/icons";
import ImageLightbox from "../../components/ImageLightbox";
import { safeUrl } from "./model";
import ScoreBadge from "./ScoreBadge";

type Props = {
  images: StudioResult[];
  videos: StudioResult[];
  /** The image of the "Make video" job running ("" when none). */
  making: string;
  /** No generation runs and the studio is ready. */
  canMake: boolean;
  /** The image whose card or videos are hovered or focused. */
  linked: string;
  onLink: (uri: string) => void;
  onMake: (uri: string) => void;
  ratio: string;
};

type CardProps = Omit<Props, "images" | "making" | "linked"> & {
  image: StudioResult;
  n: number;
  making: boolean;
  linked: boolean;
};

function ImageCard({
  image,
  n,
  videos,
  making,
  canMake,
  linked,
  onLink,
  onMake,
  ratio,
}: CardProps) {
  const [zoomed, setZoomed] = useState(false);
  const url = safeUrl(image.url);
  const uri = image.uri ?? "";
  const name = `Image ${n}`;
  const count = videos.filter((v) => uri && v.source === uri).length;
  const status = making
    ? "Making video…"
    : count
      ? `${count} video${count === 1 ? "" : "s"}`
      : "No video";
  return (
    <li
      onMouseEnter={() => onLink(uri)}
      onMouseLeave={() => onLink("")}
      onFocus={() => onLink(uri)}
      onBlur={() => onLink("")}
      className={`glass-panel rounded-2xl overflow-hidden flex flex-col animate-fade-in-up transition-shadow ${linked ? "ring-2 ring-gm-accent/60 shadow-accent-glow" : ""}`}
    >
      <div className="relative bg-black/40" style={{ aspectRatio: ratio }}>
        {url ? (
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
        ) : (
          <p className="absolute inset-0 flex items-center justify-center p-3 text-xs text-center text-gm-text-tertiary">
            Preview unavailable
          </p>
        )}
      </div>
      <div className="p-3 flex flex-col gap-2 text-sm flex-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-medium whitespace-nowrap">{name}</span>
          {typeof image.score === "number" && (
            <ScoreBadge score={image.score} />
          )}
        </div>
        {image.passed === false && (
          <span
            title="None of the try-on images passed every check: this one, the best, was animated anyway."
            className="chip self-start ring-1 ring-inset text-gm-warning bg-gm-warning/10 ring-gm-warning/25"
          >
            Didn't pass every check
          </span>
        )}
        <p className="flex items-center gap-1.5 text-caption text-gm-text-secondary">
          {making && <SpinnerIcon size={12} className="text-gm-accent" />}
          {status}
        </p>
        <button
          type="button"
          onClick={() => onMake(uri)}
          disabled={!canMake || !uri}
          aria-label={`Make video from ${name}`}
          className="glass-button rounded-lg h-8 px-3 text-xs font-medium mt-auto flex items-center justify-center gap-1.5 whitespace-nowrap"
        >
          <VideoIcon size={14} className="text-gm-accent" />
          Make video
        </button>
      </div>
      {zoomed && url && (
        <ImageLightbox src={url} alt={name} onClose={() => setZoomed(false)} />
      )}
    </li>
  );
}

/** One card per try-on image, numbered as the agent and the videos name them ("Image 1", "Image 2"...). */
export default function TryonImages({
  images,
  making,
  linked,
  ...rest
}: Props) {
  return (
    <section
      aria-labelledby="studio-tryon-images"
      className="flex flex-col gap-3"
    >
      <div className="flex items-center gap-2 min-h-[32px]">
        <h2 id="studio-tryon-images" className="text-h2">
          Try-on images
        </h2>
        <span className="chip bg-white/[0.06] text-gm-text-secondary">
          {images.length}
        </span>
      </div>
      <ul className="grid gap-3 grid-cols-2 sm:grid-cols-3 2xl:grid-cols-4">
        {images.map((image, i) => (
          <ImageCard
            // biome-ignore lint/suspicious/noArrayIndexKey: an image can have no uri yet
            key={`${image.uri ?? ""}-${i}`}
            image={image}
            n={i + 1}
            making={!!making && making === image.uri}
            linked={!!linked && linked === image.uri}
            {...rest}
          />
        ))}
      </ul>
    </section>
  );
}
