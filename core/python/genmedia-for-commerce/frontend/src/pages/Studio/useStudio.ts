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

import { useEffect, useState, useSyncExternalStore } from "react";
import { StudioStore } from "./store";
import { loadStudioId } from "./studioId";

/**
 * This browser's studio: the state the server owns, the conversation with the Studio agent and
 * the generation being followed, with the commands that change them.
 */
export function useStudio() {
  const [store] = useState(() => new StudioStore(loadStudioId()));
  useEffect(() => {
    store.start();
    return () => store.stop();
  }, [store]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot);
  return {
    ...snapshot,
    act: store.act,
    send: store.send,
    reset: store.reset,
    generate: store.jobs.generate,
    makeVideo: store.jobs.makeVideo,
    tryAgain: store.jobs.tryAgain,
    setCapability: store.setCapability,
    setSetting: store.setSetting,
    setSlot: store.items.setSlot,
    clearField: store.items.clearField,
    removeAt: store.items.removeAt,
    hasProduct: store.items.hasProduct,
    addProduct: store.items.addProduct,
    setSpin: store.items.setSpin,
    upload: store.items.upload,
  };
}

export type Studio = ReturnType<typeof useStudio>;
