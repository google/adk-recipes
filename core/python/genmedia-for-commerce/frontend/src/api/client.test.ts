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

import { describe, expect, it, vi } from "vitest";
import { ApiError, getJson, postForm, postJson, toFormData } from "./client";

function mockFetch(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("client", () => {
  it("postJson sends a JSON POST with the signal and returns the parsed body", async () => {
    const fetchMock = mockFetch(json({ items: [1] }));
    const signal = new AbortController().signal;

    const result = await postJson<{ items: number[] }>(
      "/api/x",
      { query: "q" },
      { signal },
    );

    expect(result).toEqual({ items: [1] });
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/x");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body)).toEqual({ query: "q" });
    expect(init.signal).toBe(signal);
  });

  it("getJson sends a GET and returns the parsed body", async () => {
    const fetchMock = mockFetch(json({ models: [] }));

    expect(await getJson("/api/gallery")).toEqual({ models: [] });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/gallery");
    expect(fetchMock.mock.calls[0][1].method).toBe("GET");
  });

  it("postForm posts the FormData and returns the raw response", async () => {
    const response = new Response("{}");
    const fetchMock = mockFetch(response);
    const form = new FormData();

    expect(await postForm("/api/studio/s1/uploads", form)).toBe(response);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: form,
    });
  });

  it("throws ApiError with the backend detail on a non-2xx response", async () => {
    mockFetch(json({ detail: "at most 8 product images" }, 400));

    const error = await postForm("/api/tryon", new FormData()).catch(
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 400,
      detail: "at most 8 product images",
      message: "at most 8 product images",
    });
  });

  it("joins FastAPI validation messages", async () => {
    mockFetch(
      json(
        { detail: [{ msg: "field required" }, { msg: "value too large" }] },
        422,
      ),
    );

    await expect(getJson("/api/x")).rejects.toMatchObject({
      status: 422,
      detail: "field required; value too large",
    });
  });

  it("falls back to the status when the error body has no detail", async () => {
    mockFetch(
      new Response("<html>Bad gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      }),
    );

    await expect(getJson("/api/x")).rejects.toMatchObject({
      status: 502,
      detail: "Request failed: 502 Bad Gateway",
    });
  });

  it("toFormData appends each list item, stringifies scalars and skips undefined", () => {
    const a = new File(["a"], "a.png", { type: "image/png" });
    const b = new Blob(["b"], { type: "image/jpeg" });

    const form = toFormData({
      images: [a, b],
      n: 3,
      back: false,
      text: "hi",
      none: undefined,
    });

    const images = form.getAll("images") as File[];
    expect(images).toHaveLength(2);
    expect(images[0].name).toBe("a.png");
    expect(images[1].type).toBe("image/jpeg");
    expect(form.get("n")).toBe("3");
    expect(form.get("back")).toBe("false");
    expect(form.get("text")).toBe("hi");
    expect(form.has("none")).toBe(false);
  });
});
