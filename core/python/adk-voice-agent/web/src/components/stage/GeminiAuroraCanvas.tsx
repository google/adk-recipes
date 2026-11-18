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

import { useEffect, useRef } from "react";
import type { LiveSessionStatus } from "../../lib/live-audio-client";

interface GeminiAuroraCanvasProps {
  energyRef: { readonly current: number };
  status: LiveSessionStatus;
}

/** Uses ResizeObserver + `Math.ceil` logical dimensions so there is never a 1px seam on the stage border. */
export function GeminiAuroraCanvas({
  energyRef,
  status,
}: GeminiAuroraCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const statusRef = useRef<LiveSessionStatus>(status);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const isConnected =
    status === "listening" || status === "speaking" || status === "held";

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animId = 0;
    let phase = 0;
    let smoothedEnergy = 0;
    let logicalW = 0;
    let logicalH = 0;
    const styles = getComputedStyle(document.documentElement);
    const rgb = (name: string) => styles.getPropertyValue(name).trim();
    const palette = {
      royal: rgb("--aurora-royal-rgb"),
      navy: rgb("--aurora-navy-rgb"),
      blue: rgb("--aurora-blue-rgb"),
      sky: rgb("--aurora-sky-rgb"),
      azure: rgb("--aurora-azure-rgb"),
      ice: rgb("--aurora-ice-rgb"),
      light: rgb("--aurora-light-rgb"),
      fade: rgb("--aurora-fade-rgb"),
    };

    const updateSize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      logicalW = Math.ceil(rect.width);
      logicalH = Math.ceil(rect.height);
      canvas.width = logicalW * dpr;
      canvas.height = logicalH * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    updateSize();
    const ro = new ResizeObserver(updateSize);
    ro.observe(canvas);
    window.addEventListener("resize", updateSize);

    const render = () => {
      const w = logicalW;
      const h = logicalH;
      ctx.clearRect(0, 0, w, h);

      if (statusRef.current === "idle" || statusRef.current === "connecting") {
        smoothedEnergy = 0;
        animId = requestAnimationFrame(render);
        return;
      }

      const rawE = statusRef.current === "held" ? 0 : energyRef.current;
      smoothedEnergy = smoothedEnergy * 0.88 + rawE * 0.12;

      // Advance lateral drift when there is voice input/output energy
      phase += 0.002 + smoothedEnergy * 0.018;

      // Soft radial plumes floating near the bottom of the stage (no hard rectangle edges)
      const lift = smoothedEnergy * h * 0.14;
      const radiusScale = 1 + smoothedEnergy * 0.45;
      const baseAlpha =
        statusRef.current === "held" ? 0.2 : 0.48 + smoothedEnergy * 0.42;

      const blobs = [
        {
          x: w * 0.5,
          y: h * 0.96 - lift * 0.6,
          rx: w * 0.78,
          ry: h * (0.24 + smoothedEnergy * 0.16),
          colorInner: `rgba(${palette.royal}, ${baseAlpha * 0.72})`,
          colorMid: `rgba(${palette.navy}, ${baseAlpha * 0.32})`,
        },
        {
          x: w * (0.28 + Math.sin(phase) * 0.06),
          y: h * 0.91 - lift * 0.85,
          rx: w * 0.46 * radiusScale,
          ry: h * 0.22 * radiusScale,
          colorInner: `rgba(${palette.blue}, ${(0.42 + smoothedEnergy * 0.45) * baseAlpha})`,
          colorMid: `rgba(${palette.royal}, ${(0.18 + smoothedEnergy * 0.22) * baseAlpha})`,
        },
        {
          x: w * (0.72 + Math.cos(phase * 0.85) * 0.06),
          y: h * 0.89 - lift,
          rx: w * 0.5 * radiusScale,
          ry: h * 0.24 * radiusScale,
          colorInner: `rgba(${palette.sky}, ${(0.38 + smoothedEnergy * 0.52) * baseAlpha})`,
          colorMid: `rgba(${palette.azure}, ${(0.2 + smoothedEnergy * 0.25) * baseAlpha})`,
        },
        {
          x: w * 0.5,
          y: h * 0.93 - lift * 1.05,
          rx: w * 0.38 * radiusScale,
          ry: h * 0.18 * radiusScale,
          colorInner: `rgba(${palette.ice}, ${(0.3 + smoothedEnergy * 0.55) * baseAlpha})`,
          colorMid: `rgba(${palette.light}, ${(0.16 + smoothedEnergy * 0.28) * baseAlpha})`,
        },
      ];

      ctx.save();
      ctx.globalCompositeOperation = "screen";
      for (const b of blobs) {
        ctx.save();
        ctx.translate(b.x, b.y);
        ctx.scale(1, b.ry / b.rx);
        const g = ctx.createRadialGradient(0, 0, b.rx * 0.04, 0, 0, b.rx);
        g.addColorStop(0, b.colorInner);
        g.addColorStop(0.52, b.colorMid);
        g.addColorStop(1, `rgba(${palette.fade}, 0)`);
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(0, 0, b.rx, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
      ctx.restore();

      animId = requestAnimationFrame(render);
    };

    animId = requestAnimationFrame(render);
    return () => {
      cancelAnimationFrame(animId);
      ro.disconnect();
      window.removeEventListener("resize", updateSize);
    };
  }, [energyRef]);

  return (
    <canvas
      ref={canvasRef}
      className="gemini-aurora-canvas"
      style={{
        opacity: isConnected ? 1 : 0,
        transition: "opacity 0.55s ease",
      }}
    />
  );
}
