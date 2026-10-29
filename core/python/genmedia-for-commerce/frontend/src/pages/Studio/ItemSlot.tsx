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

import {
  Children,
  useState,
  type ComponentType,
  type DragEvent,
  type ReactNode,
} from "react";
import type { StudioItem } from "../../api/studio";
import { CloseIcon, SpinnerIcon, UploadIcon } from "../../components/icons";
import { safeUrl } from "./model";
import { ACCEPT } from "./shrink";

/** The studio's image (its thumbnail, or its label until one is known). */
function ItemThumb({
  item,
  className = "",
}: {
  item: StudioItem;
  className?: string;
}) {
  const src = safeUrl(item.thumb);
  return src ? (
    <img
      src={src}
      alt={item.label}
      className={`w-full h-full object-contain ${className}`}
    />
  ) : (
    <span className="w-full h-full flex items-center justify-center p-1 text-[10px] text-center text-gm-text-tertiary animate-pulse-soft">
      {item.label}
    </span>
  );
}

export type CardIcon = ComponentType<{ size?: number; className?: string }>;

/** The title row of a studio card: its icon, title and what is on the right (a count, actions). */
export function CardHeader({
  icon: Icon,
  title,
  hint,
  children,
}: {
  icon: CardIcon;
  title: string;
  hint?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="shrink-0 w-8 h-8 rounded-lg bg-gm-accent/10 ring-1 ring-inset ring-gm-accent/20 text-gm-accent flex items-center justify-center">
        <Icon size={16} />
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-2 min-h-[32px]">
          <h3 className="text-h3">{title}</h3>
          {children && (
            <span className="flex items-center gap-2">{children}</span>
          )}
        </div>
        {hint && (
          <p className="text-caption font-normal text-gm-text-secondary">
            {hint}
          </p>
        )}
      </div>
    </div>
  );
}

/** The small remove button over a thumbnail (always shown on touch screens). */
function RemoveButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title="Remove"
      onClick={onClick}
      className="absolute top-1.5 right-1.5 w-6 h-6 rounded-full bg-black/70 ring-1 ring-white/20 text-white flex items-center justify-center hover:bg-gm-error/90 sm:opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-all"
    >
      <CloseIcon size={12} />
    </button>
  );
}

type ItemSlotProps = {
  label: string;
  icon: CardIcon;
  hint: string;
  items: StudioItem[];
  max: number;
  onRemove: (index: number) => void;
  /** Removes every item (lists). */
  onClear?: () => void;
  /** Images dropped or picked: uploaded to the studio. */
  onFiles: (files: File[]) => void;
  /** An upload into this card is in flight (a photo of several MB takes a while on a phone). */
  uploading?: boolean;
  /** Source buttons (gallery, catalogue), shown while there is room. */
  children?: ReactNode;
  /** Shown at the bottom, also when the card is full (e.g. options of its image). */
  footer?: ReactNode;
};

type UploadZoneProps = {
  label: string;
  room: number;
  multiple: boolean;
  uploading: boolean;
  onFiles: (files: File[]) => void;
};

/** Drop images or click to pick them, up to `room` (`label` names the field for screen readers); a spinner while
 * an upload is in flight (announced by the studio's status line). */
function UploadZone({
  label,
  room,
  multiple,
  uploading,
  onFiles,
}: UploadZoneProps) {
  const [dragging, setDragging] = useState(false);
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    onFiles([...event.dataTransfer.files].slice(0, room));
  };
  return (
    <label
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={`upload-zone !min-h-[104px] !p-4 !gap-2 cursor-pointer focus-within:ring-2 focus-within:ring-gm-accent ${dragging ? "drag-over" : ""}`}
    >
      <span className="w-9 h-9 rounded-full bg-gm-accent/10 text-gm-accent flex items-center justify-center">
        {uploading ? <SpinnerIcon size={17} /> : <UploadIcon size={17} />}
      </span>
      <span className="text-sm text-gm-text-primary">
        {uploading ? (
          "Uploading…"
        ) : (
          <>
            Drop {multiple ? "images" : "an image"} or{" "}
            <span className="text-gm-accent font-medium">click to upload</span>
          </>
        )}
      </span>
      <span className="text-[11px] text-gm-text-tertiary">
        PNG, JPEG, WebP, HEIC or AVIF
      </span>
      <input
        type="file"
        accept={ACCEPT}
        multiple={multiple}
        className="sr-only"
        aria-label={`Upload ${label}`}
        onChange={(e) => {
          onFiles([...(e.target.files ?? [])].slice(0, room));
          e.target.value = "";
        }}
      />
    </label>
  );
}

/** A card of the studio's images in one field: the server's items, removable, plus upload and pickers. */
export default function ItemSlot({
  label,
  icon,
  hint,
  items,
  max,
  onRemove,
  onClear,
  onFiles,
  uploading = false,
  children,
  footer,
}: ItemSlotProps) {
  const room = max - items.length;
  const count =
    max > 1 ? `${items.length}/${max}` : items.length ? "Set" : "Empty";

  return (
    <div className="glass-panel studio-card">
      <CardHeader icon={icon} title={label} hint={hint}>
        {onClear && items.length > 1 && (
          <button
            type="button"
            onClick={onClear}
            aria-label={`Clear ${label}`}
            className="text-caption text-gm-text-tertiary hover:text-gm-text-primary transition-colors"
          >
            Clear
          </button>
        )}
        <span
          className={`chip ${items.length ? "bg-gm-accent/10 text-gm-accent" : "bg-white/[0.06] text-gm-text-tertiary"}`}
        >
          {count}
        </span>
      </CardHeader>

      {items.length > 0 && (
        <ul
          className={`grid gap-2.5 ${max > 1 ? "grid-cols-4" : "grid-cols-[repeat(2,minmax(0,1fr))] sm:grid-cols-[repeat(2,minmax(0,160px))]"}`}
        >
          {items.map((item, i) => (
            <li
              // biome-ignore lint/suspicious/noArrayIndexKey: the same image can be in the list twice
              key={`${item.ref}-${i}`}
              className="flex flex-col gap-1.5 min-w-0 animate-scale-in"
              title={item.label}
            >
              <div className="relative group aspect-square rounded-xl overflow-hidden bg-black/30 ring-1 ring-white/10">
                <ItemThumb item={item} />
                <RemoveButton
                  label={`Remove ${item.label}`}
                  onClick={() => onRemove(i)}
                />
              </div>
              <span className="text-[11px] text-gm-text-secondary truncate">
                {item.label}
              </span>
            </li>
          ))}
        </ul>
      )}

      {room > 0 && (
        <div className="flex flex-col gap-2.5">
          <UploadZone
            label={label}
            room={room}
            multiple={max > 1}
            uploading={uploading}
            onFiles={onFiles}
          />
          {Children.toArray(children).length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-caption font-normal text-gm-text-tertiary">
                or choose from
              </span>
              {children}
            </div>
          )}
        </div>
      )}
      {footer}
    </div>
  );
}
