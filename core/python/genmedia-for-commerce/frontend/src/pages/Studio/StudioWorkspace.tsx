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

import type { ComponentType } from "react";
import type { StudioCapability } from "../../api/studio";
import {
  CheckIcon,
  ImageIcon,
  SparkleIcon,
  SpinIcon,
  SpinnerIcon,
  VideoIcon,
} from "../../components/icons";
import { CAP_KEYS, CAPS, missingInput } from "./model";
import { ModelField, ProductsField, SpinField } from "./StudioInputs";
import StudioResults from "./StudioResults";
import { MotionField, RunOptionsPanel, StyleFields } from "./StudioSettings";
import type { Studio } from "./useStudio";

type Props = { studio: Studio };

const CAP_ICONS: Record<
  StudioCapability,
  ComponentType<{ size?: number; className?: string }>
> = {
  tryon: ImageIcon,
  video_tryon: VideoIcon,
  spin: SpinIcon,
};

/** Photo Try-On / Video Try-On / 360° Spin, and whether the studio is saving. */
function CapabilityTabs({ studio }: Props) {
  const busy = studio.saving || studio.uploading.length > 0;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div
        role="tablist"
        aria-label="Capability"
        className="bg-white/[0.03] border border-white/[0.07] rounded-xl p-1 grid grid-cols-3 sm:flex gap-1 w-full sm:w-auto"
      >
        {CAP_KEYS.map((cap) => {
          const Icon = CAP_ICONS[cap];
          const on = studio.view.capability === cap;
          return (
            <button
              key={cap}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => studio.setCapability(cap)}
              className={`flex items-center justify-center gap-2 px-2 sm:px-4 py-2 rounded-lg text-button whitespace-nowrap transition-all ${
                on
                  ? "bg-white/[0.09] text-gm-text-primary shadow-level-2 ring-1 ring-inset ring-white/10"
                  : "text-gm-text-secondary hover:text-gm-text-primary hover:bg-white/[0.04]"
              }`}
            >
              <Icon
                size={15}
                className={`hidden sm:block ${on ? "text-gm-accent" : ""}`}
              />
              {CAPS[cap]}
            </button>
          );
        })}
      </div>
      <span
        role="status"
        className="flex items-center gap-1.5 text-caption text-gm-text-tertiary empty:hidden sm:min-h-[20px]"
      >
        {busy ? (
          <>
            <SpinnerIcon size={13} />{" "}
            {studio.uploading.length ? "Uploading…" : "Saving…"}
          </>
        ) : (
          studio.ready &&
          !studio.notice && (
            <span className="hidden sm:inline-flex items-center gap-1.5">
              <CheckIcon size={13} className="text-gm-success" /> Saved
            </span>
          )
        )}
      </span>
    </div>
  );
}

/** Generate the current page; the running generation's progress, or why it cannot start. */
function GenerateBar({ studio }: Props) {
  const cap = studio.view.capability;
  const busy = !!studio.view.job || studio.following;
  const jobCap =
    studio.following && studio.live
      ? studio.live.cap
      : studio.view.job?.capability;
  const running = busy && jobCap === cap;
  const missing = missingInput(studio.view, cap);
  const status =
    studio.jobError ||
    (running
      ? studio.jobStatus
      : busy
        ? `A ${CAPS[jobCap as StudioCapability]} generation is running.`
        : missing);
  const retry = studio.retry && studio.jobError && (
    <button
      type="button"
      onClick={() => void studio.tryAgain()}
      disabled={busy || studio.starting || !studio.ready}
      className="glass-button shrink-0 h-8 px-3 rounded-lg text-xs font-medium text-gm-text-primary"
    >
      Try again
    </button>
  );
  // Desktop: stays in view at the bottom of a long column of inputs.
  return (
    <div className="flex flex-col gap-2 lg:sticky lg:bottom-4 z-10 rounded-2xl lg:p-3 lg:-mx-3 lg:bg-gm-bg/85 lg:backdrop-blur-md lg:shadow-[0_-16px_32px_-16px_rgba(0,0,0,0.9)]">
      <button
        type="button"
        onClick={() => void studio.generate()}
        disabled={busy || studio.starting || !!missing || !studio.ready}
        className="btn-primary w-full flex items-center justify-center gap-2 !py-3.5 text-[15px]"
      >
        {running ? <SpinnerIcon size={16} /> : <SparkleIcon size={16} />}
        {running ? "Generating…" : `Generate ${CAPS[cap]}`}
      </button>
      {status && (
        <div className="flex items-center justify-center gap-3">
          <p
            role={studio.jobError ? "alert" : "status"}
            className={`text-caption text-center ${studio.jobError ? "text-gm-error" : "text-gm-text-tertiary"}`}
          >
            {status}
          </p>
          {retry}
        </div>
      )}
    </div>
  );
}

function PageInputs({ studio }: Props) {
  const { capability } = studio.view;
  if (capability === "spin") return <SpinField studio={studio} />;
  // Photo Try-On and Video Try-On take the same inputs (the video starts with that try-on).
  return (
    <>
      <ModelField studio={studio} field="person" />
      <ProductsField studio={studio} />
      <ModelField studio={studio} field="reference_model" />
      <StyleFields studio={studio} />
      {capability === "video_tryon" && <MotionField studio={studio} />}
    </>
  );
}

/** The studio the server owns: the page's inputs, settings, Generate and results. */
export default function StudioWorkspace({ studio }: Props) {
  const notice = studio.restoreMessage || studio.notice;
  return (
    <div className="flex flex-col gap-6 min-w-0">
      <CapabilityTabs studio={studio} />
      {notice && (
        <p
          role="alert"
          className="text-sm text-gm-error bg-gm-error/10 border border-gm-error/25 rounded-xl px-4 py-2.5"
        >
          {notice}
        </p>
      )}
      <div className="grid gap-6 xl:gap-8 xl:grid-cols-[minmax(0,400px)_minmax(0,1fr)] items-start">
        {/* Busy until the saved studio was read. */}
        <section
          className="flex flex-col gap-4"
          aria-label="Inputs"
          aria-busy={!studio.ready}
        >
          <PageInputs studio={studio} />
          <RunOptionsPanel studio={studio} />
          <GenerateBar studio={studio} />
        </section>
        <section className="flex flex-col gap-6 min-w-0" aria-label="Results">
          <StudioResults studio={studio} />
        </section>
      </div>
    </div>
  );
}
