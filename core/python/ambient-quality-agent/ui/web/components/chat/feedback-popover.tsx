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

import { CircleHelp } from "lucide-react";

import appConfig from "@/app-config.json";
import { Button } from "@/components/ui/button";

interface FeedbackPopoverProps {
  sessionId?: string;
}

export function FeedbackPopover(_props: FeedbackPopoverProps) {
  // The /feedback endpoint returns 501, so the button links directly to the
  // issue tracker configured for the deployment in app-config.json.
  return (
    <Button
      asChild
      variant="ghost"
      size="icon"
      className="h-8 w-8 text-muted-foreground hover:text-foreground"
    >
      <a
        href={appConfig.feedbackUrl}
        target="_blank"
        rel="noreferrer"
        aria-label="Send feedback"
        title="Have a suggestion?"
      >
        <CircleHelp className="h-4 w-4" />
      </a>
    </Button>
  );
}
