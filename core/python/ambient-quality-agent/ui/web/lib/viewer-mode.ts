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

/**
 * The read-only viewer switch. A host that shows finished investigations
 * serves the shell with `<meta name="aqua-viewer" content="1">` in its head;
 * the page then keeps nothing past its own life (index.html's first script)
 * and hides chat, configuration and every control that writes
 * (`isViewerMode`).
 */

const VIEWER_META = 'meta[name="aqua-viewer"][content="1"]';

/** Returns true if the page was served by a read-only viewer. The tag is
 *  fixed for the page's life, so it is read on each call rather than cached,
 *  which lets a test add and remove it. */
export function isViewerMode(): boolean {
  return document.querySelector(VIEWER_META) !== null;
}
