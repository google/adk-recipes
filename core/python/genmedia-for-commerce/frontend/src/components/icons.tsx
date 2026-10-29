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

// The inline SVG icons used in more than one place. Stroke icons inherit currentColor.
import type { ReactNode } from "react";

type IconProps = { size?: number; className?: string };

function Stroke({
  size = 16,
  className,
  children,
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export const CloseIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M18 6L6 18M6 6L18 18" />
  </Stroke>
);
export const CheckIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M5 12l5 5L20 7" />
  </Stroke>
);
export const SearchIcon = (p: IconProps) => (
  <Stroke {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="M20 20l-4-4" />
  </Stroke>
);
export const UploadIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M2 17v2a2 2 0 002 2h16a2 2 0 002-2v-2" />
    <path d="M12 15V3M12 3L8 7M12 3L16 7" />
  </Stroke>
);
export const DownloadIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M2 17v2a2 2 0 002 2h16a2 2 0 002-2v-2" />
    <path d="M12 3v12M12 15l-4-4M12 15l4-4" />
  </Stroke>
);
export const SparkleIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M12 3c.6 5 3.4 8.4 9 9-5.6.6-8.4 4-9 9-.6-5-3.4-8.4-9-9 5.6-.6 8.4-4 9-9z" />
  </Stroke>
);
export const PlusIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M12 5v14M5 12h14" />
  </Stroke>
);
export const SendIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M12 19V5M5 12l7-7 7 7" />
  </Stroke>
);
export const ChevronIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M6 9l6 6 6-6" />
  </Stroke>
);
export const ImageIcon = (p: IconProps) => (
  <Stroke {...p}>
    <rect x="3" y="3" width="18" height="18" rx="3" />
    <circle cx="9" cy="9" r="2" />
    <path d="M21 15l-5-5L5 21" />
  </Stroke>
);
export const VideoIcon = (p: IconProps) => (
  <Stroke {...p}>
    <rect x="2" y="6" width="14" height="12" rx="2" />
    <path d="M16 10l6-3v10l-6-3" />
  </Stroke>
);
export const SpinIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M21 12a9 9 0 11-3-6.7" />
    <path d="M21 3v5h-5" />
  </Stroke>
);
export const UserIcon = (p: IconProps) => (
  <Stroke {...p}>
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0116 0" />
  </Stroke>
);
export const TagIcon = (p: IconProps) => (
  <Stroke {...p}>
    <path d="M20.6 13.4l-7.2 7.2a2 2 0 01-2.8 0L3 13V3h10l7.6 7.6a2 2 0 010 2.8z" />
    <circle cx="8" cy="8" r="1.5" />
  </Stroke>
);
export const GridIcon = (p: IconProps) => (
  <Stroke {...p}>
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
  </Stroke>
);
export const StarIcon = ({ size = 12, className }: IconProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="currentColor"
    className={className}
    aria-hidden="true"
  >
    <path d="M12 2.5l2.9 6 6.6.8-4.9 4.5 1.3 6.5L12 17l-5.9 3.3 1.3-6.5L2.5 9.3l6.6-.8z" />
  </svg>
);

export function SpinnerIcon({ size = 20, className = "" }: IconProps) {
  return (
    <svg
      className={`animate-spin ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
        fill="none"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );
}
