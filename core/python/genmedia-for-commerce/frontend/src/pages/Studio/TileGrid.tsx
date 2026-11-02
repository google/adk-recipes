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

import { CheckIcon } from "../../components/icons";
import { safeUrl } from "./model";

/** `meta`: a second line under the label (e.g. a spin set's number of views). */
export type Tile = { key: string; label: string; thumb: string; meta?: string };

/** Models are portraits; product photos keep their whole outline on white. */
const IMAGE = {
  portrait: "aspect-[3/4] object-cover object-top bg-white/5",
  product: "aspect-square object-contain p-2 bg-white",
};

type TileGridProps = {
  tiles: Tile[];
  kind: keyof typeof IMAGE;
  onPick: (index: number) => void;
  /** Tiles already in the studio (checked, not picked again). */
  isPicked?: (index: number) => boolean;
  /** No room left for another pick. */
  full?: boolean;
};

/** Gallery or catalogue images (refs with thumbnails) to pick for the studio. */
export default function TileGrid({
  tiles,
  kind,
  onPick,
  isPicked = () => false,
  full = false,
}: TileGridProps) {
  return (
    <div className="grid grid-cols-2 min-[420px]:grid-cols-3 sm:grid-cols-4 gap-3 max-h-[60vh] overflow-y-auto p-0.5 -m-0.5">
      {tiles.map((tile, i) => {
        const picked = isPicked(i);
        return (
          <button
            key={tile.key}
            type="button"
            aria-label={tile.label}
            title={tile.label}
            aria-pressed={picked}
            disabled={picked || full}
            onClick={() => onPick(i)}
            className={`group relative rounded-xl overflow-hidden bg-white/[0.04] ring-1 transition-all text-left disabled:cursor-not-allowed ${
              picked
                ? "ring-2 ring-gm-accent"
                : "ring-white/10 enabled:hover:ring-gm-accent/60 enabled:hover:-translate-y-0.5 enabled:hover:shadow-level-3"
            } ${full && !picked ? "opacity-50" : ""}`}
          >
            <img
              src={safeUrl(tile.thumb) || undefined}
              alt=""
              loading="lazy"
              decoding="async"
              className={`block w-full ${IMAGE[kind]}`}
            />
            {picked && (
              <span className="absolute top-2 right-2 w-6 h-6 rounded-full bg-gm-accent text-gm-bg flex items-center justify-center shadow-level-2">
                <CheckIcon size={12} />
              </span>
            )}
            <span className="block px-2.5 pt-2 pb-2 text-xs font-medium truncate">
              {tile.label}
            </span>
            {tile.meta && (
              <span className="block px-2.5 -mt-1.5 pb-2 text-[11px] text-gm-text-tertiary">
                {tile.meta}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
