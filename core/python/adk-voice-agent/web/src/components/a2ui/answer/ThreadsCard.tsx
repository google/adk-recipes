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

import { gmailThreadUrl, senderName } from "../../../lib/answerCards";
import type { ThreadItem } from "../../../lib/cards";
import { timeAgo } from "../../../lib/format";
import { keyedByText } from "../../../lib/keys";
import { Caption, Row, Rows } from "../context/parts";

/** Email threads: who, when, the subject, and a line of the message; unread ones stand out. */
export function ThreadsCard({ items }: { items: ThreadItem[] }) {
  const unread = items.filter((t) => t.unread).length;
  return (
    <>
      {unread > 0 && <Caption>{unread} unread</Caption>}
      <Rows>
        {keyedByText(items, (t) => t.id).map(({ key, item: thread }) => (
          <Row key={key} href={gmailThreadUrl(thread.id)}>
            <div className="mail-main" data-unread={thread.unread || undefined}>
              <p className="mail-top">
                <span className="mail-sender">
                  {senderName(thread.from) || "(Unknown sender)"}
                </span>
                {(thread.message_count ?? 0) > 1 && (
                  <span className="mail-count">{thread.message_count}</span>
                )}
                <span className="mail-time">{timeAgo(thread.date)}</span>
                {thread.unread && (
                  <span className="mail-dot" role="img" aria-label="Unread" />
                )}
              </p>
              <p className="mail-subject">{thread.subject || "(No subject)"}</p>
              {thread.snippet && (
                <p className="mail-snippet">{thread.snippet}</p>
              )}
            </div>
          </Row>
        ))}
      </Rows>
    </>
  );
}
