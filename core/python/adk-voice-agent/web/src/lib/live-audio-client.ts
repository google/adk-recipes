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

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMe } from "./api";
import { latency } from "./latency";

export type LiveSessionStatus =
  | "idle"
  | "connecting"
  | "listening"
  | "speaking"
  | "held"
  | "mic_blocked"
  | "error";

/** Whether the socket is up, so a text turn goes into this session instead of starting one. */
export const isConnected = (status: LiveSessionStatus) =>
  status === "listening" ||
  status === "speaking" ||
  status === "held" ||
  status === "mic_blocked";

export interface LiveAudioState {
  status: LiveSessionStatus;
  substatus: string;
  /**
   * 0 (silent) to 1 (loud), from mic input and agent output. Updated every animation
   * frame without re-rendering; read it from an animation loop.
   */
  audioEnergyRef: { readonly current: number };
  activeToolCall: string | null;
  startSession: (initialPrompt?: string) => Promise<void>;
  toggleHold: () => void;
  endSession: () => void;
  sendTextTurn: (text: string) => void;
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function base64ToInt16Array(base64: string): Int16Array {
  const normalized = base64.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Int16Array(bytes.buffer);
}

/** What the UI hears from a session besides audio. */
export interface LiveEventHandlers {
  /** The model called a tool (the UI follows `navigate`). */
  onToolCall?: (call: { name: string; args: Record<string, unknown> }) => void;
  /** A tool's result is out, so its card exists now. */
  onToolResponse?: (result: {
    name: string;
    response: Record<string, unknown>;
  }) => void;
  /** The model finished its turn. */
  onTurnComplete?: () => void;
}

export function useGeminiLiveAudio(
  handlers: LiveEventHandlers = {},
): LiveAudioState {
  const [status, setStatus] = useState<LiveSessionStatus>("idle");
  const [substatus, setSubstatus] = useState<string>(
    "Tap Live to connect voice",
  );
  const audioEnergyRef = useRef<number>(0);
  const [activeToolCall, setActiveToolCall] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const micCtxRef = useRef<AudioContext | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const nextPlayTimeRef = useRef<number>(0);
  const activeSourcesRef = useRef<AudioBufferSourceNode[]>([]);
  const heldRef = useRef<boolean>(false);
  const rafRef = useRef<number>(0);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const outputEnergyRef = useRef<number>(0);
  // The caller passes fresh callbacks each render; the socket handlers read the
  // latest ones through this ref so `startSession` stays stable.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  const flushPlaybackQueue = useCallback(() => {
    for (const src of activeSourcesRef.current) {
      src.onended = null;
      try {
        src.stop();
      } catch {
        // ignore already stopped
      }
    }
    activeSourcesRef.current = [];
    outputEnergyRef.current = 0;
    if (audioCtxRef.current) {
      nextPlayTimeRef.current = audioCtxRef.current.currentTime;
    }
  }, []);

  const playPcm24kChunk = useCallback((int16: Int16Array) => {
    const ctx = audioCtxRef.current;
    if (!ctx || heldRef.current) return;

    const float32 = new Float32Array(int16.length);
    let sumSq = 0;
    for (let i = 0; i < int16.length; i++) {
      const sample = int16[i] / 32768;
      float32[i] = sample;
      sumSq += sample * sample;
    }
    const rms = Math.min(1, Math.sqrt(sumSq / Math.max(1, int16.length)) * 5.0);
    outputEnergyRef.current = Math.max(outputEnergyRef.current * 0.5, rms);

    const buffer = ctx.createBuffer(1, float32.length, 24000);
    buffer.getChannelData(0).set(float32);

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);

    const startAt = Math.max(ctx.currentTime, nextPlayTimeRef.current);
    source.start(startAt);
    nextPlayTimeRef.current = startAt + buffer.duration;
    activeSourcesRef.current.push(source);
    setStatus("speaking");

    source.onended = () => {
      activeSourcesRef.current = activeSourcesRef.current.filter(
        (s) => s !== source,
      );
      if (activeSourcesRef.current.length === 0 && !heldRef.current) {
        outputEnergyRef.current = 0;
        setStatus("listening");
      }
    };
  }, []);

  // Measure real-time microphone input + agent output energy via WebAudio AnalyserNode
  const startAudioMeterLoop = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    const dataArray = new Uint8Array(128);

    const tick = () => {
      if (heldRef.current) {
        audioEnergyRef.current = 0;
        rafRef.current = requestAnimationFrame(tick);
        return;
      }

      let micLevel = 0;
      if (analyserRef.current) {
        analyserRef.current.getByteTimeDomainData(dataArray);
        let sumSq = 0;
        for (let i = 0; i < dataArray.length; i++) {
          const norm = (dataArray[i] - 128) / 128;
          sumSq += norm * norm;
        }
        const rms = Math.sqrt(sumSq / dataArray.length);
        // Noise gate so background room hum doesn't jitter the gradient
        micLevel = rms < 0.015 ? 0 : Math.min(1, (rms - 0.015) * 7.5);
      }

      // Decay output energy smoothly
      outputEnergyRef.current *= 0.92;
      const combined = Math.max(micLevel, outputEnergyRef.current);

      audioEnergyRef.current = audioEnergyRef.current * 0.82 + combined * 0.18;
      rafRef.current = requestAnimationFrame(tick);
    };

    rafRef.current = requestAnimationFrame(tick);
  }, []);

  /** Release everything a session holds: playback, the mic, both audio contexts, the meter. */
  const teardown = useCallback(
    (
      nextStatus: "idle" | "error" = "idle",
      nextSubstatus = "Tap Live to connect voice",
    ) => {
      cancelAnimationFrame(rafRef.current);
      flushPlaybackQueue();
      if (processorRef.current) {
        processorRef.current.onaudioprocess = null;
        processorRef.current.disconnect();
        processorRef.current = null;
      }
      if (mediaStreamRef.current) {
        for (const t of mediaStreamRef.current.getTracks()) t.stop();
        mediaStreamRef.current = null;
      }
      micCtxRef.current?.close().catch(() => {});
      micCtxRef.current = null;
      audioCtxRef.current?.close().catch(() => {});
      audioCtxRef.current = null;
      analyserRef.current = null;
      heldRef.current = false;
      audioEnergyRef.current = 0;
      setActiveToolCall(null);
      setStatus(nextStatus);
      setSubstatus(nextSubstatus);
    },
    [flushPlaybackQueue],
  );

  const startSession = useCallback(
    async (initialPrompt?: string) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        heldRef.current = false;
        if (mediaStreamRef.current) {
          for (const track of mediaStreamRef.current.getAudioTracks()) {
            track.enabled = true;
          }
        }
        setStatus("listening");
        if (initialPrompt) {
          wsRef.current.send(
            JSON.stringify({
              content: { role: "user", parts: [{ text: initialPrompt }] },
            }),
          );
        }
        return;
      }

      setStatus("connecting");
      setSubstatus("Connecting to Gemini Live...");

      try {
        // iOS Safari requires AudioContext creation and resume inside the synchronous tap handler.
        const ctx =
          audioCtxRef.current && audioCtxRef.current.state !== "closed"
            ? audioCtxRef.current
            : new AudioContext({ sampleRate: 24000 });
        audioCtxRef.current = ctx;
        nextPlayTimeRef.current = ctx.currentTime;
        void ctx.resume().catch(() => {});

        const micCtx =
          micCtxRef.current && micCtxRef.current.state !== "closed"
            ? micCtxRef.current
            : new AudioContext({ sampleRate: 16000 });
        micCtxRef.current = micCtx;
        void micCtx.resume().catch(() => {});

        const sessionId = `live-${Date.now()}`;
        // The server binds ADK's routes to the signed-in user, so ask it who that is.
        const { user_id } = await fetchMe();
        const userId = encodeURIComponent(user_id);
        await fetch(`/apps/app/users/${userId}/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId }),
        }).catch(() => {});

        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${proto}//${window.location.host}/run_live?app_name=app&user_id=${userId}&session_id=${sessionId}`;
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = async () => {
          heldRef.current = false;
          setStatus("listening");
          setSubstatus("Gemini Live · Listening");

          if (initialPrompt && ws.readyState === WebSocket.OPEN) {
            ws.send(
              JSON.stringify({
                content: { role: "user", parts: [{ text: initialPrompt }] },
              }),
            );
          }

          try {
            const stream = await navigator.mediaDevices.getUserMedia({
              audio: {
                sampleRate: 16000,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
              },
            });
            if (wsRef.current !== ws) {
              for (const t of stream.getTracks()) t.stop();
              return;
            }
            mediaStreamRef.current = stream;

            const activeMicCtx =
              micCtxRef.current && micCtxRef.current.state !== "closed"
                ? micCtxRef.current
                : new AudioContext({ sampleRate: 16000 });
            micCtxRef.current = activeMicCtx;
            void activeMicCtx.resume().catch(() => {});
            const source = activeMicCtx.createMediaStreamSource(stream);

            const analyser = activeMicCtx.createAnalyser();
            analyser.fftSize = 256;
            analyser.smoothingTimeConstant = 0.75;
            source.connect(analyser);
            analyserRef.current = analyser;

            const processor = activeMicCtx.createScriptProcessor(2048, 1, 1);
            processorRef.current = processor;
            source.connect(processor);
            processor.connect(activeMicCtx.destination);

            processor.onaudioprocess = (e) => {
              if (heldRef.current || ws.readyState !== WebSocket.OPEN) return;
              const input = e.inputBuffer.getChannelData(0);
              const pcm16 = new Int16Array(input.length);
              for (let i = 0; i < input.length; i++) {
                const s = Math.max(-1, Math.min(1, input[i]));
                pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
              }
              ws.send(
                JSON.stringify({
                  blob: {
                    mime_type: "audio/pcm;rate=16000",
                    data: arrayBufferToBase64(pcm16.buffer),
                  },
                }),
              );
            };

            startAudioMeterLoop();
          } catch {
            setStatus("mic_blocked");
            setSubstatus("Microphone blocked: allow it in your browser");
            startAudioMeterLoop();
          }
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg?.interrupted || msg?.serverContent?.interrupted) {
              flushPlaybackQueue();
              setStatus("listening");
              return;
            }

            const parts =
              msg?.content?.parts || msg?.serverContent?.modelTurn?.parts || [];
            for (const part of parts) {
              if (part.functionCall?.name) {
                latency.call(part.functionCall);
                setActiveToolCall(part.functionCall.name);
                handlersRef.current.onToolCall?.({
                  name: part.functionCall.name,
                  args: part.functionCall.args ?? {},
                });
              }
              if (part.functionResponse) {
                latency.response(part.functionResponse);
                setActiveToolCall(null);
                handlersRef.current.onToolResponse?.({
                  name: part.functionResponse.name ?? "",
                  response: part.functionResponse.response ?? {},
                });
              }
              const inline = part.inlineData || part.inline_data;
              if (
                inline?.data &&
                String(inline.mimeType || inline.mime_type || "").includes(
                  "audio/pcm",
                )
              ) {
                latency.audio();
                playPcm24kChunk(base64ToInt16Array(inline.data));
              }
            }
            if (msg?.turnComplete || msg?.serverContent?.turnComplete) {
              latency.turnComplete();
              handlersRef.current.onTurnComplete?.();
            }
          } catch {
            // ignore non-json frames
          }
        };

        ws.onclose = () => {
          // A socket closed via endSession() or replaced by a newer session must not report an error.
          if (wsRef.current !== ws) return;
          wsRef.current = null;
          teardown("error", "Couldn't connect. Tap Live to try again.");
        };
      } catch {
        wsRef.current = null;
        teardown("error", "Couldn't connect. Tap Live to try again.");
      }
    },
    [flushPlaybackQueue, playPcm24kChunk, startAudioMeterLoop, teardown],
  );

  const toggleHold = useCallback(() => {
    const held = !heldRef.current;
    heldRef.current = held;
    if (mediaStreamRef.current) {
      for (const track of mediaStreamRef.current.getAudioTracks()) {
        track.enabled = !held;
      }
    }
    if (held) {
      flushPlaybackQueue();
      audioEnergyRef.current = 0;
      setStatus("held");
      setSubstatus("On Hold · Tap Resume to continue");
    } else {
      setStatus("listening");
      setSubstatus("Gemini Live · Listening");
    }
  }, [flushPlaybackQueue]);

  const endSession = useCallback(() => {
    const ws = wsRef.current;
    wsRef.current = null;
    ws?.close();
    teardown();
  }, [teardown]);

  const sendTextTurn = useCallback(
    (text: string) => {
      outputEnergyRef.current = 0.65;
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            content: {
              role: "user",
              parts: [{ text }],
            },
          }),
        );
      } else {
        void startSession(text);
      }
    },
    [startSession],
  );

  useEffect(() => {
    return () => {
      endSession();
    };
  }, [endSession]);

  return {
    status,
    substatus,
    audioEnergyRef,
    activeToolCall,
    startSession,
    toggleHold,
    endSession,
    sendTextTurn,
  };
}
