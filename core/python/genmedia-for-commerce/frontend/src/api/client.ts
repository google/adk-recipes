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

// The only module that calls fetch. Every non-2xx response becomes an ApiError.

export type RequestOptions = { signal?: AbortSignal };

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  /** Builds the error from a FastAPI `{detail}` body, falling back to the HTTP status. */
  static async fromResponse(response: Response): Promise<ApiError> {
    const fallback =
      `Request failed: ${response.status} ${response.statusText}`.trim();
    let detail: unknown;
    try {
      detail = (await response.json())?.detail;
    } catch {
      detail = undefined;
    }
    if (Array.isArray(detail)) {
      detail = detail
        .map((item) => item?.msg ?? JSON.stringify(item))
        .join("; ");
    }
    return new ApiError(
      response.status,
      typeof detail === "string" && detail ? detail : fallback,
    );
  }
}

async function send(path: string, init: RequestInit): Promise<Response> {
  const response = await fetch(path, init);
  if (!response.ok) throw await ApiError.fromResponse(response);
  return response;
}

export function postForm(
  path: string,
  body: FormData,
  { signal }: RequestOptions = {},
): Promise<Response> {
  return send(path, { method: "POST", body, signal });
}

export async function postJson<T>(
  path: string,
  body: unknown,
  { signal }: RequestOptions = {},
): Promise<T> {
  const init = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  };
  return (await send(path, init)).json();
}

export async function getJson<T>(
  path: string,
  { signal }: RequestOptions = {},
): Promise<T> {
  return (await send(path, { method: "GET", signal })).json();
}

type FormValue =
  | Blob
  | Blob[]
  | string
  | string[]
  | number
  | boolean
  | undefined;

/** Multipart body: one entry per list item; undefined fields are left out. */
export function toFormData(fields: Record<string, FormValue>): FormData {
  const form = new FormData();
  for (const [name, value] of Object.entries(fields)) {
    if (value === undefined) continue;
    for (const item of Array.isArray(value) ? value : [value]) {
      form.append(name, item instanceof Blob ? item : String(item));
    }
  }
  return form;
}
