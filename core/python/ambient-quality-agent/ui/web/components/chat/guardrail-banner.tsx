"use client";
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

import { AlertTriangle } from "lucide-react";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

interface GuardrailBannerProps {
  contextId?: string | null;
  bootError: string | null;
}

export function GuardrailBanner({ bootError }: GuardrailBannerProps) {
  if (!bootError) return null;

  return (
    <div
      className={cn(
        textStyle.meta,
        "flex items-center gap-2 border-b border-destructive/40 bg-destructive/10 px-4 py-2 text-destructive",
      )}
    >
      <AlertTriangle className="h-3.5 w-3.5" />
      <span className={cn(textStyle.label, "text-destructive")}>
        Disconnected
      </span>
      <span className="text-destructive/80">{bootError}</span>
    </div>
  );
}
