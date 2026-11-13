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

import { X } from "lucide-react";
import { type ReactNode, useRef } from "react";
import { useModalFocus } from "../common/useModalFocus";

/**
 * Slide-up panel over the stage; tapping the backdrop or the close button calls `onClose`.
 * With `page`, it is a full-screen page that slides in from the right instead (Settings).
 */
export function BottomSheet({
  testId,
  header,
  closeLabel,
  bodyClassName,
  page = false,
  onClose,
  children,
}: {
  testId: string;
  /** Left side of the header: a title or a back button. */
  header: ReactNode;
  closeLabel: string;
  bodyClassName?: string;
  page?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = `${testId}-title`;
  useModalFocus(panel, onClose);

  return (
    <div className="sheet-backdrop">
      {/* Tapping outside the panel closes the sheet. It's a pointer shortcut, so
          it stays out of the tab order and the accessibility tree; the labeled
          close button below is the accessible way out. */}
      <button
        type="button"
        className="sheet-scrim"
        tabIndex={-1}
        aria-hidden
        onClick={onClose}
      />
      <div
        ref={panel}
        className="bottom-sheet-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-testid={testId}
        data-page={page || undefined}
      >
        <div className="sheet-header">
          <div id={titleId}>{header}</div>
          <button
            type="button"
            className="sheet-close-btn"
            onClick={onClose}
            aria-label={closeLabel}
          >
            <X size={16} />
          </button>
        </div>
        <div
          className={
            bodyClassName ? `sheet-body ${bodyClassName}` : "sheet-body"
          }
        >
          {children}
        </div>
      </div>
    </div>
  );
}
