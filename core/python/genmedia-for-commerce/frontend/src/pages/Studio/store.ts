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

// The client side of a studio, ported from the MCP App panel (mcp/static/studio.html):
// the server owns the state; every user change is sent as an action, one at a time and
// in order, and the page renders the newest state it got back (`updated_at`).
import {
  agentTurn,
  getStudioGallery,
  getStudioMessages,
  getStudioPreviews,
  getStudioState,
  sendStudioAction,
  type ActionResult,
  type ChatMessage,
  type JobSpec,
  type StudioAction,
  type StudioCapability,
  type StudioGallery,
  type StudioItem,
  type StudioResult,
} from "../../api/studio";
import { ApiError } from "../../api/client";
import { errorMessage } from "../../shared/files";
import { ItemCommands, type UploadField } from "./items";
import { JobFollower } from "./jobs";
import {
  blank,
  fromServer,
  refsWithoutThumb,
  withThumbs,
  type Setting,
  type StudioView,
  type UrlCache,
} from "./model";
import { newStudioId } from "./studioId";

/**
 * The generation being followed: its results so far and (Video Try-On) the try-on images it animated so far;
 * `tryonImage`: the image of a "Make video" job, whose results replace only that image's videos ("" for a full run).
 */
export type LiveJob = {
  cap: StudioCapability;
  results: StudioResult[];
  images: StudioResult[];
  tryonImage: string;
};
export type ChatEntry = ChatMessage & { error?: boolean };

export type StudioSnapshot = {
  studioId: string;
  view: StudioView;
  /** The saved studio was read: until then nothing is written over it. */
  ready: boolean;
  restoreMessage: string;
  /** Why the last action was not saved. */
  notice: string;
  saving: boolean;
  /** The field of each upload in flight. */
  uploading: UploadField[];
  live: LiveJob | null;
  /** A generation is being started (its job id is not known yet). */
  starting: boolean;
  following: boolean;
  jobStatus: string;
  jobError: string;
  /** The spec of the last generation started or followed here, when it ended in error ("Try again"). */
  retry: JobSpec | null;
  gallery: StudioGallery | null;
  galleryError: string;
  messages: ChatEntry[];
  /** The saved conversation was read (or could not be): until then nothing is sent. */
  messagesLoaded: boolean;
  agentBusy: boolean;
};

/** A setting being typed: its save runs when the typing pauses, or on stop. */
type Typing = {
  value: Setting;
  timer?: ReturnType<typeof setTimeout>;
  save: () => void;
};

const RESTORE_RETRY_S = [2, 4, 8, 16];
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const isObj = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);

export class StudioStore {
  private snap: StudioSnapshot;
  private readonly listeners = new Set<() => void>();
  /** `updated_at` of the state shown. */
  private at = 0;
  /** Bumped by stop and reset: answers for an earlier session are dropped. */
  private epochs = 0;
  private restoreRun = 0;
  private queue: Promise<unknown> = Promise.resolve();
  private queued = 0;
  /** "cap/key" -> a setting being typed, not saved yet: it stays over the states adopted meanwhile. */
  private readonly typing = new Map<string, Typing>();
  /** Catalogue refs -> their gs:// copies in the studio. */
  readonly copies = new Map<string, string>();
  private readonly asked = new Set<string>();
  readonly urls: UrlCache = new Map();
  readonly jobs = new JobFollower(this);
  readonly items = new ItemCommands(this);

  constructor(studioId: string) {
    this.snap = {
      studioId,
      view: blank(),
      ready: false,
      restoreMessage: "",
      notice: "",
      saving: false,
      uploading: [],
      live: null,
      starting: false,
      following: false,
      jobStatus: "",
      jobError: "",
      retry: null,
      gallery: null,
      galleryError: "",
      messages: [],
      messagesLoaded: false,
      agentBusy: false,
    };
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = () => this.snap;

  /** The current session: it changes on stop and reset. */
  get epoch(): number {
    return this.epochs;
  }

  get state(): StudioSnapshot {
    return this.snap;
  }

  set(patch: Partial<StudioSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((listener) => {
      listener();
    });
  }

  /** Load the saved studio, its conversation and the gallery (models and spin sets). */
  start(): void {
    void this.restore();
    void this.loadMessages();
    if (!this.snap.gallery) void this.loadGallery();
  }

  /** Stop polling and retries (the page is closing); settings being typed are saved now, to this studio. */
  stop(): void {
    this.typing.forEach((entry) => {
      if (entry.timer === undefined) return;
      clearTimeout(entry.timer);
      entry.save();
    });
    this.typing.clear();
    this.epochs++;
    this.restoreRun++;
    this.jobs.stop();
  }

  /** "New": a new studio and conversation; the old one stays saved on the server. */
  reset = () => {
    this.stop();
    this.at = 0;
    this.set({
      studioId: newStudioId(),
      view: blank(),
      ready: true,
      restoreMessage: "",
      notice: "",
      uploading: [],
      live: null,
      starting: false,
      following: false,
      jobStatus: "",
      jobError: "",
      retry: null,
      messages: [],
      messagesLoaded: true,
      agentBusy: false,
    });
  };

  /** Show a state from the server unless a newer one is shown (`strict`: only a strictly newer one). */
  adopt(st: unknown, updatedAt: unknown, strict = false): boolean {
    const t = Number(updatedAt) || 0;
    if (!isObj(st) || (strict ? t <= this.at : t < this.at)) return false;
    this.at = t;
    const next = fromServer(st, this.urls);
    for (const [k, entry] of this.typing) {
      const [cap, key] = k.split("/") as [StudioCapability, string];
      next.settings[cap][key] = entry.value;
    }
    this.set(this.jobs.polling ? { view: next } : { view: next, live: null });
    void this.fillThumbs();
    this.jobs.follow();
    return true;
  }

  /** The server's state wins (after a failed action, or a result without a state); a studio never saved is blank. */
  async refresh(): Promise<void> {
    const epoch = this.epoch;
    try {
      const saved = await getStudioState(this.snap.studioId);
      if (epoch === this.epoch && typeof saved.saved === "boolean")
        this.adopt(saved.saved ? saved.state : {}, saved.updated_at);
    } catch {
      // The next action or result brings the state.
    }
  }

  /** Send one user action; actions run in order and only the last answer is rendered. */
  act = (action: StudioAction): Promise<ActionResult | null> => {
    if (!this.snap.ready) return Promise.resolve(null);
    const { epoch } = this;
    const studioId = this.snap.studioId;
    if (++this.queued === 1) this.set({ saving: true });
    const run = this.queue.then(async () => {
      try {
        const r = await sendStudioAction(studioId, action);
        if (epoch !== this.epoch) return null;
        this.carryThumbs(r.refs);
        this.set({ notice: "" });
        if (this.queued === 1) this.adopt(r.state, r.updated_at);
        return r;
      } catch (e) {
        if (epoch !== this.epoch) return null;
        this.set({ notice: `Not saved: ${errorMessage(e)}` });
        if (this.queued === 1) await this.refresh();
        return null;
      } finally {
        if (--this.queued === 0) this.set({ saving: false });
      }
    });
    this.queue = run.catch(() => undefined);
    return run;
  };

  setCapability = (cap: StudioCapability) => {
    if (this.snap.view.capability === cap) return;
    this.set({ view: { ...this.snap.view, capability: cap } });
    void this.act({ capability: cap });
  };

  /** Show a setting at once and save it (`debounceMs` while it is typed). */
  setSetting = (
    cap: StudioCapability,
    key: string,
    value: Setting,
    debounceMs = 0,
  ) => {
    const { view } = this.snap;
    this.set({
      view: {
        ...view,
        settings: {
          ...view.settings,
          [cap]: { ...view.settings[cap], [key]: value },
        },
      },
    });
    const k = `${cap}/${key}`;
    clearTimeout(this.typing.get(k)?.timer);
    const entry: Typing = {
      value,
      save: () => {
        entry.timer = undefined;
        void this.act({ settings: { [key]: value }, page: cap }).finally(() => {
          if (this.typing.get(k) === entry) this.typing.delete(k);
        });
      },
    };
    this.typing.set(k, entry);
    entry.timer = setTimeout(entry.save, debounceMs);
  };

  /** One message to the agent; its reply and the studio it saved are shown. */
  send = async (text: string) => {
    const message = text.trim();
    if (!message || this.snap.agentBusy || !this.snap.messagesLoaded) return;
    const { epoch } = this;
    this.set({
      messages: [...this.snap.messages, { role: "user", text: message }],
      agentBusy: true,
    });
    try {
      const turn = await agentTurn(this.snap.studioId, message);
      if (epoch !== this.epoch) return;
      this.set({
        messages: [...this.snap.messages, { role: "agent", text: turn.reply }],
      });
      // Shown when newer than the page's; a turn whose save failed carries no state: show the server's.
      if (this.snap.ready) {
        if (isObj(turn.state)) this.adopt(turn.state, turn.updated_at, true);
        else await this.refresh();
      }
    } catch (e) {
      if (epoch === this.epoch)
        this.set({
          messages: [
            ...this.snap.messages,
            { role: "agent", text: errorMessage(e), error: true },
          ],
        });
    } finally {
      if (epoch === this.epoch) this.set({ agentBusy: false });
    }
  };

  /** Keep an image's thumbnail for the states that bring its ref back. */
  remember(item: StudioItem): void {
    if (item.ref && item.thumb) this.urls.set(item.ref, item.thumb);
  }

  /** Catalogue picks come back as gs:// copies: they keep their thumbnails. */
  private carryThumbs(refs: ActionResult["refs"]): void {
    for (const [from, to] of Object.entries(isObj(refs) ? refs : {})) {
      if (typeof to !== "string") continue;
      this.copies.set(from, to);
      const thumb = this.urls.get(from);
      if (thumb) this.urls.set(to, thumb);
    }
  }

  private async fillThumbs(): Promise<void> {
    const missing = refsWithoutThumb(this.snap.view).filter(
      (ref) => !this.urls.has(ref) && !this.asked.has(ref),
    );
    if (!missing.length) return;
    missing.forEach((ref) => {
      this.asked.add(ref);
    });
    try {
      const { previews } = await getStudioPreviews(missing);
      for (const [ref, url] of Object.entries(isObj(previews) ? previews : {}))
        if (typeof url === "string") this.urls.set(ref, url);
    } catch {
      // Thumbnails are cosmetic: asked again on the next state.
      missing.forEach((ref) => {
        this.asked.delete(ref);
      });
    }
    this.set({ view: withThumbs(this.snap.view, this.urls) });
  }

  /** Read the saved studio; a failed read is retried and never taken for a new studio. */
  private async restore(): Promise<void> {
    const run = ++this.restoreRun;
    this.at = 0;
    this.set({ ready: false });
    for (let i = 0; ; i++) {
      let reason = "";
      const saved = await getStudioState(this.snap.studioId).catch(
        (error: unknown) => {
          reason =
            error instanceof ApiError && error.status !== 404
              ? error.detail
              : "";
          return null;
        },
      );
      if (run !== this.restoreRun) return;
      if (saved && typeof saved.saved === "boolean") {
        this.set({ ready: true, restoreMessage: "" });
        if (saved.saved) this.adopt(saved.state, saved.updated_at);
        return;
      }
      const last = i >= RESTORE_RETRY_S.length;
      const because = reason ? ` ${reason}` : "";
      this.set({
        restoreMessage: last
          ? `Could not load the saved studio, so changes are off.${because} Reload the page to try again.`
          : `Could not load the saved studio, retrying…${because}`,
      });
      if (last) return;
      await sleep(RESTORE_RETRY_S[i] * 1000);
      if (run !== this.restoreRun) return;
    }
  }

  /** The saved conversation; nothing is sent before it is shown, so it replaces the (empty) local one. */
  private async loadMessages(): Promise<void> {
    const { epoch } = this;
    try {
      const { messages } = await getStudioMessages(this.snap.studioId);
      if (epoch === this.epoch) this.set({ messages, messagesLoaded: true });
    } catch {
      if (epoch === this.epoch) this.set({ messagesLoaded: true }); // An empty conversation: the studio itself still loads.
    }
  }

  private async loadGallery(): Promise<void> {
    try {
      const gallery = await getStudioGallery();
      gallery.models.forEach((m) => {
        this.remember(m);
      });
      this.set({ gallery, galleryError: "" });
    } catch (e) {
      this.set({ galleryError: errorMessage(e) });
    }
  }
}
