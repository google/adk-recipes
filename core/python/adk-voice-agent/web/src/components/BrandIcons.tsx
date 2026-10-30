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

import { Blocks, type LucideIcon, SquareTerminal } from "lucide-react";

/** Logos by the brand key the server sends (an app's or a harness's `icon`, a card's tool). */
const BRAND_ASSETS: Record<string, { src: string; alt: string }> = {
  google: { src: "/assets/google.svg", alt: "Google" },
  google_workspace: { src: "/assets/google.svg", alt: "Google" },
  google_search: { src: "/assets/google.svg", alt: "Google Search" },
  google_maps: { src: "/assets/maps.webp", alt: "Google Maps" },
  github: { src: "/assets/github.png", alt: "GitHub" },
  claude: { src: "/assets/claude-ai.svg", alt: "Claude Code" },
  antigravity: { src: "/assets/antigravity.png", alt: "Antigravity" },
  adk: { src: "/assets/adk-logo.png", alt: "ADK Voice Agent" },
};

function BrandImage({ brand, size }: { brand: string; size: number }) {
  const { src, alt } = BRAND_ASSETS[brand];
  return (
    <img
      src={src}
      alt={alt}
      width={size}
      height={size}
      style={{
        width: size,
        height: size,
        objectFit: "contain",
        display: "block",
      }}
    />
  );
}

/** A logo by its brand key; any other key gets the neutral `fallback`. */
export function BrandIcon({
  icon,
  size = 18,
  fallback: Fallback = Blocks,
}: {
  icon: string;
  size?: number;
  fallback?: LucideIcon;
}) {
  if (!Object.hasOwn(BRAND_ASSETS, icon))
    return <Fallback size={size} aria-hidden className="icon-muted" />;
  return <BrandImage brand={icon} size={size} />;
}

/** A harness's logo; an unknown harness gets a neutral terminal. */
export function HarnessBrandIcon({
  icon,
  size = 18,
}: {
  icon: string;
  size?: number;
}) {
  return <BrandIcon icon={icon} size={size} fallback={SquareTerminal} />;
}

export function AdkBrandEmblem({ size = 38 }: { size?: number }) {
  return <BrandImage brand="adk" size={size} />;
}
