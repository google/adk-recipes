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

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import logo from "../assets/logo.png";

/** The top bar: the brand, linking to the Studio (the app's only page), and the page's actions on the right. */
function TopNav({ children }: { children?: ReactNode }) {
  return (
    // Z-index hierarchy: TopNav (z-50) > Content (z-0)
    <header className="px-4 sm:px-6 lg:px-8 h-14 border-b border-white/[0.06] bg-gm-bg/70 backdrop-blur-glass sticky top-0 z-50">
      <div className="max-w-[1440px] h-full mx-auto flex items-center gap-4">
        <Link to="/" className="flex items-center gap-3 group rounded-lg">
          <img
            src={logo}
            alt="GenMedia for Commerce"
            className="h-8 w-auto object-contain rounded-lg transition-transform duration-300 group-hover:scale-105"
          />
          <span className="hidden sm:flex items-baseline gap-1.5 text-[15px] tracking-tight">
            <span className="font-semibold">GenMedia</span>
            <span className="text-gm-text-secondary">for Commerce</span>
          </span>
        </Link>
        <span className="chip bg-gm-accent/10 text-gm-accent border border-gm-accent/20">
          Studio
        </span>
        <div className="ml-auto flex items-center gap-2">{children}</div>
      </div>
    </header>
  );
}

export default TopNav;
