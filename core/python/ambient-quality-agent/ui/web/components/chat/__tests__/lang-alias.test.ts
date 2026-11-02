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
import { prismLanguageFromInfo } from "@/components/chat/tool-views";

describe("prismLanguageFromInfo", () => {
  it("maps common coding-agent languages + aliases", () => {
    expect(prismLanguageFromInfo("go")).toBe("go");
    expect(prismLanguageFromInfo("golang")).toBe("go");
    expect(prismLanguageFromInfo("kt")).toBe("kotlin");
    expect(prismLanguageFromInfo("terraform")).toBe("hcl");
    expect(prismLanguageFromInfo("c#")).toBe("csharp");
    expect(prismLanguageFromInfo("YAML")).toBe("yaml"); // case-insensitive
  });

  it("falls back to markup for unknown / empty", () => {
    expect(prismLanguageFromInfo("brainfuck")).toBe("markup");
    expect(prismLanguageFromInfo("")).toBe("markup");
    expect(prismLanguageFromInfo(null)).toBe("markup");
  });
});
