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

import { useNavigate, useRouter } from "@tanstack/react-router";
import { MessageSquare } from "lucide-react";
import { Button, type ButtonProps } from "@/components/ui/button";
import { insightRcaPrompt } from "@/components/insights/insight-rca-prompt";
import { prefetchAgentCard } from "@/lib/a2a-client";

interface DiagnoseInChatButtonProps {
  /** Unique insight ID included in the initial prompt for agent lookup. */
  insightId: string;
  /** Triage signature matched by the chat agent. */
  label: string;
  /** Accessible button label for screen readers. */
  ariaLabel: string;
  /** Renders only the icon without text for dense layouts. */
  iconOnly?: boolean;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
  className?: string;
}

/**
 * Renders a button that opens a chat conversation preloaded with a root-cause
 * diagnosis prompt for the given insight.
 *
 * Stops click propagation to prevent triggering parent link navigation when
 * placed inside clickable cards or rail items.
 *
 * @param props - Component properties.
 * @returns Button navigating to chat.
 */
export function DiagnoseInChatButton({
  insightId,
  label,
  ariaLabel,
  iconOnly = false,
  variant = "ghost",
  size = "sm",
  className,
}: DiagnoseInChatButtonProps) {
  const navigate = useNavigate();
  const router = useRouter();

  const warmHover = () => {
    prefetchAgentCard();
    void router.preloadRoute({ to: "/c" }).catch(() => {});
  };

  return (
    <Button
      size={size}
      variant={variant}
      onMouseEnter={warmHover}
      onFocus={prefetchAgentCard}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        void navigate({
          to: "/c",
          search: { q: insightRcaPrompt(insightId, label) },
        });
      }}
      className={className}
      aria-label={ariaLabel}
    >
      <MessageSquare />
      {!iconOnly && "Chat"}
    </Button>
  );
}
