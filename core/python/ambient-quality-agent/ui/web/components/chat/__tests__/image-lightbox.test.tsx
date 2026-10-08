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
import userEvent from "@testing-library/user-event";
import {
  ImageLightboxProvider,
  useImageLightbox,
} from "@/components/chat/image-lightbox";

function Consumer() {
  const open = useImageLightbox();
  return (
    <button type="button" onClick={() => open("blob:abc", "a cat")}>
      show
    </button>
  );
}

describe("ImageLightbox", () => {
  it("opens a dialog showing the requested image", async () => {
    render(
      <ImageLightboxProvider>
        <Consumer />
      </ImageLightboxProvider>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "show" }));
    const img = screen.getByRole("img", { name: "a cat" });
    expect(img).toHaveAttribute("src", "blob:abc");
  });

  it("provides a no-op opener when used outside a provider (no crash)", () => {
    // useImageLightbox falls back to a no-op so consumers never throw.
    function Bare() {
      const open = useImageLightbox();
      return <button onClick={() => open("x")}>ok</button>;
    }
    render(<Bare />);
    expect(screen.getByText("ok")).toBeInTheDocument();
  });
});
