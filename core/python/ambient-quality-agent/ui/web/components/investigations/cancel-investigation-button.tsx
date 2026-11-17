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

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Square } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  buildAgentScope,
  cancelInvestigation,
  investigationQuery,
  investigationsQuery,
} from "@/lib/aqua-api";
import { useAgentKey } from "@/lib/agent-selection";
import { textStyle } from "@/lib/typography";
import { cn } from "@/lib/utils";

/**
 * Cancel investigation: stops a run that has not finished, after the user
 * confirms. A cancel cannot be undone, and a running sweep stops where it is
 * without creating insights, so the dialog says so before anything is sent.
 *
 * The caller decides whether the run can still be cancelled (`isCancellable`);
 * the agent refuses one that finished meanwhile, and the dialog shows why.
 */
export function CancelInvestigationButton({
  investigationId,
}: {
  investigationId: string;
}) {
  const agentKey = useAgentKey();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  // The run's record is what says where it stands, and the list and health
  // badge read the same records.
  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: investigationQuery(agentKey, investigationId).queryKey,
    });
    void queryClient.invalidateQueries({
      queryKey: investigationsQuery(agentKey).queryKey,
    });
    void queryClient.invalidateQueries({
      queryKey: [...buildAgentScope(agentKey), "health"],
    });
  };

  const cancel = useMutation({
    mutationFn: () => cancelInvestigation(agentKey, investigationId),
    onSuccess: () => {
      setOpen(false);
      refresh();
    },
  });
  const busy = cancel.isPending;

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return;
        if (next) {
          cancel.reset();
        } else if (cancel.isError) {
          // Refreshed only once the refusal has been read: a run that
          // finished meanwhile would unmount this dialog, and its reason.
          refresh();
        }
        setOpen(next);
      }}
    >
      <AlertDialogTrigger asChild>
        <Button
          size="sm"
          variant="outline"
          className="h-7 border-destructive/40 px-2 text-destructive hover:bg-destructive/10 hover:text-destructive"
        >
          <Square className="h-3 w-3 fill-current" />
          Cancel investigation
        </Button>
      </AlertDialogTrigger>
      {open && (
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cancel this investigation?</AlertDialogTitle>
            <AlertDialogDescription>
              It stops where it is and creates no insights. Anything it already
              stored stays. This can&rsquo;t be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {cancel.error && (
            <div
              role="alert"
              className={cn(
                textStyle.meta,
                "rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-destructive",
              )}
            >
              Couldn&rsquo;t cancel it:{" "}
              {cancel.error instanceof Error
                ? cancel.error.message
                : String(cancel.error)}
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Keep running</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              onClick={(e) => {
                // Keep the dialog open until the agent answers, so a refusal
                // is shown where the user asked.
                e.preventDefault();
                cancel.mutate();
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {busy ? "Cancelling…" : "Cancel investigation"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}
