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
  useEffect,
  useEffectEvent,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { CloseIcon } from "./icons";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])';

/** Opaque panel used by every dialog box. */
export const DIALOG_PANEL =
  "bg-[#141416] border border-white/[0.1] shadow-level-3";

type DialogProps = {
  label: string;
  onClose: () => void;
  className?: string;
  children: ReactNode;
};

/**
 * Modal overlay rendered into document.body, so no blurred or transformed ancestor can clip it.
 * Focus moves inside on open and back to the opener on close, Tab stays inside, Escape and a
 * click on the backdrop close it.
 */
export function Dialog({
  label,
  onClose,
  className = "",
  children,
}: DialogProps) {
  const panel = useRef<HTMLDivElement>(null);
  // Read on the first render, before an autoFocus field inside takes the focus.
  const [opener] = useState(() => document.activeElement as HTMLElement | null);
  const close = useEffectEvent(onClose);

  useEffect(() => {
    const node = panel.current as HTMLDivElement;
    const focusables = () => [...node.querySelectorAll<HTMLElement>(FOCUSABLE)];
    // An autoFocus field inside wins; otherwise the first control (usually Close).
    if (!node.contains(document.activeElement))
      (focusables()[0] ?? node).focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === "Tab") {
        const items = focusables();
        const first = items[0] ?? node;
        const last = items[items.length - 1] ?? node;
        const active = document.activeElement;
        if (
          !node.contains(active) ||
          active === (event.shiftKey ? first : last)
        ) {
          event.preventDefault();
          (event.shiftKey ? last : first).focus();
        }
      }
    };
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      opener?.focus();
    };
  }, [opener]);

  return createPortal(
    // biome-ignore lint/a11y/noStaticElementInteractions lint/a11y/useKeyWithClickEvents: a click on the backdrop closes it; Escape does too (onKey)
    <div
      className="fixed inset-0 z-[10000] flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm animate-fade-in"
      onClick={onClose}
    >
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: keeps a click inside from reaching the backdrop */}
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className={`outline-none ${className}`}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}

type ModalProps = {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
};

/** Titled dialog box with a Close button; scrolls when its content is taller than the viewport. */
export default function Modal({
  isOpen,
  onClose,
  title,
  children,
}: ModalProps) {
  if (!isOpen) return null;
  return (
    <Dialog
      label={title}
      onClose={onClose}
      className={`${DIALOG_PANEL} w-full max-w-2xl max-h-full overflow-y-auto rounded-2xl p-5 sm:p-8 animate-scale-in`}
    >
      <div className="flex items-center justify-between gap-4 mb-6">
        <h2 className="text-h1 font-bold">{title}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="w-10 h-10 shrink-0 rounded-lg hover:bg-white/10 flex items-center justify-center transition-all"
        >
          <CloseIcon size={20} className="text-gm-text-primary" />
        </button>
      </div>
      {children}
    </Dialog>
  );
}
