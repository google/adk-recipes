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

import { useState } from "react";
import { SparkleIcon } from "../../components/icons";
import PageShell from "../../components/PageShell";
import ChatPanel from "./ChatPanel";
import StudioWorkspace from "./StudioWorkspace";
import { useStudio } from "./useStudio";

/** The app: the studio the server owns, with the Studio agent's conversation on the side. */
export default function Studio() {
  const studio = useStudio();
  // Phones: the chat is a drawer.
  const [chatOpen, setChatOpen] = useState(false);

  return (
    <PageShell
      title="Studio"
      subtitle="Create product content by chatting with the studio agent, or set it up yourself."
      columns="lg:grid-cols-[minmax(0,1fr)_380px]"
    >
      <StudioWorkspace studio={studio} />
      <ChatPanel
        messages={studio.messages}
        loaded={studio.messagesLoaded}
        busy={studio.agentBusy}
        onSend={(text) => void studio.send(text)}
        onNew={studio.reset}
        open={chatOpen}
        onClose={() => setChatOpen(false)}
      />
      <button
        type="button"
        onClick={() => setChatOpen(true)}
        aria-expanded={chatOpen}
        aria-label="Open the studio agent"
        className="lg:hidden fixed bottom-5 right-5 z-50 flex items-center gap-2 h-12 px-5 rounded-full bg-primary-gradient text-[#0B1220] text-button font-semibold shadow-accent-lg ring-1 ring-white/20"
      >
        <SparkleIcon size={16} /> Studio agent
      </button>
    </PageShell>
  );
}
