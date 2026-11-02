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

import { MessageSquarePlus } from "lucide-react";
import { Button } from "@/components/ui/button";

interface NewConversationMenuProps {
  onGeneral: () => void;
}

export function NewConversationMenu({ onGeneral }: NewConversationMenuProps) {
  // AQuA has no projects: a conversation is an A2A context and nothing groups
  // them. The menu collapses to the one action that applies: New chat.
  return (
    <Button
      type="button"
      variant="outline"
      className="nc-trigger h-9 w-full justify-start gap-2"
      title="New chat"
      onClick={onGeneral}
    >
      <MessageSquarePlus className="h-4 w-4 shrink-0" />
      <span className="nc-label truncate">New chat</span>
    </Button>
  );
}
