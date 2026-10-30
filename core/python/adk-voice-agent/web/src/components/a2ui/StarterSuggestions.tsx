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

import { Mic } from "lucide-react";
import type { ReactNode } from "react";
import type { StarterSuggestion } from "../../lib/starters";
import { AdkBrandEmblem } from "../BrandIcons";

/**
 * Idle stage: a quiet brand lockup and things to say, anchored above the Live
 * button. `children` (the Connect card) sits between them.
 */
export function StarterSuggestions({
  suggestions,
  onSelect,
  children,
}: {
  /** Only what the connected apps can do (`starterSuggestions`). */
  suggestions: StarterSuggestion[];
  onSelect: (prompt: string) => void;
  children?: ReactNode;
}) {
  return (
    <div className="starter" data-testid="starter-suggestions-centered">
      <div className="starter-brand">
        <AdkBrandEmblem size={72} />
        <p className="starter-name">ADK Voice Agent</p>
        <p className="starter-tagline">
          A live voice agent for work in motion.
        </p>
      </div>

      {children}

      <section className="starter-try" aria-label="Try saying">
        <p className="starter-label">Try saying</p>
        <ul className="starter-list">
          {suggestions.map(({ prompt, meta }) => (
            <li key={prompt}>
              <button
                type="button"
                className="starter-row"
                onClick={() => onSelect(prompt)}
              >
                <span className="starter-text">
                  <span className="starter-prompt">“{prompt}”</span>
                  <span className="starter-meta">{meta}</span>
                </span>
                <Mic size={15} className="starter-mic" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
