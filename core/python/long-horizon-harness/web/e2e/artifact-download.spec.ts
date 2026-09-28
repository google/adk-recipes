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

import { expect, test } from "@playwright/test";
import { mockA2A } from "./helpers/mock-a2a";

// Locks the just-fixed regression at the UI layer: when the agent emits a
// FilePart with file.name="damped_oscillator.png" the chat bubble must render
// that exact filename, NOT the "attachment.<ext>" fallback that
// message-bubble.tsx::defaultFileName() returns when file.name is missing.
//
// If app/tools/artifacts.py drops Blob.display_name again, the A2A stream
// emits FilePart.file.name=null, the alt text falls back to "attachment.png",
// and the second assertion in this spec catches it.
test("image filename from FilePart renders in chat (not 'attachment.png' fallback)", async ({
  page,
}) => {
  await mockA2A(page, { streams: ["artifact-save.jsonl"] });

  await page.goto("/");
  await page.getByRole("textbox").fill("save the plot");
  await page.keyboard.press("Enter");

  // The image is rendered via <img alt={name}>. Use getByAltText so we hit the
  // exact field that defaultFileName() would otherwise have clobbered.
  // A received file surfaces as an attachment chip labelled with the FilePart's
  // filename; opening it shows the image in the right-hand artifact viewer.
  await expect(
    page.getByText("damped_oscillator.png", { exact: true }),
  ).toBeVisible({ timeout: 10_000 });

  // The filename must come from the part, not the generic mime-derived default.
  await expect(page.getByText(/^attachment\.png$/)).toHaveCount(0);
});
