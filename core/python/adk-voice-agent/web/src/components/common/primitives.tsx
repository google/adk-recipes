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

import { type ReactNode, useEffect, useState } from "react";

/** A group of controls under a small uppercase label; `aside` sits at the label's right. */
export function Section({
  label,
  aside,
  children,
}: {
  label: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="sheet-section">
      <div className="sheet-section-head">
        <div className="section-label">{label}</div>
        {aside}
      </div>
      {children}
    </div>
  );
}

/** One line of text that crossfades whenever it changes. */
export function LiveLine({ text }: { text: string }) {
  const [lines, setLines] = useState([{ id: 0, text }]);
  useEffect(() => {
    setLines((prev) => {
      const last = prev[prev.length - 1];
      return last.text === text ? prev : [last, { id: last.id + 1, text }];
    });
  }, [text]);
  return (
    <span className="fl-live">
      {lines.map((line, i) =>
        i === lines.length - 1 ? (
          <span key={line.id} className="fl-live-in">
            {line.text}
          </span>
        ) : (
          <span
            key={line.id}
            className="fl-live-out"
            aria-hidden
            onAnimationEnd={() => setLines((prev) => prev.slice(-1))}
          >
            {line.text}
          </span>
        ),
      )}
    </span>
  );
}
