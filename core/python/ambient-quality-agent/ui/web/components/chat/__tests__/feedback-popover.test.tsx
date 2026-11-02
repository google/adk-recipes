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

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import appConfig from "@/app-config.json";
import { FeedbackPopover } from "../feedback-popover";

// AQuA replaced the rating popover with a link: `/feedback` answers 501 here,
// so collecting a thumb and a comment would drop them on the floor.
describe("FeedbackPopover", () => {
  it("opens the issue tracker app-config.json names rather than collecting a rating", () => {
    render(<FeedbackPopover />);
    const link = screen.getByRole("link", { name: "Send feedback" });
    expect(appConfig.feedbackUrl).toMatch(/^https:\/\//);
    // The issue tracker rejects blank issues, so the URL must specify a template.
    expect(appConfig.feedbackUrl).toMatch(/[?&]template=/);
    expect(link).toHaveAttribute("href", appConfig.feedbackUrl);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("keeps the suggestion affordance discoverable", () => {
    render(<FeedbackPopover />);
    expect(screen.getByTitle("Have a suggestion?")).toBeInTheDocument();
  });
});
