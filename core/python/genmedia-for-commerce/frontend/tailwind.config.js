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

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // GenMedia Design System Colors - Refined
        "gm-bg": "#050505",
        "gm-surface": "#0A0A0A",
        // Opaque panels: drawers and dialogs over the page.
        "gm-raised": "#121317",
        "gm-text-primary": "#FFFFFF",
        "gm-text-secondary": "#A1A1AA",
        // 5.5:1 on gm-bg (WCAG AA for small text).
        "gm-text-tertiary": "#8B8B94",
        "gm-accent": "#8AB4F8",
        "gm-accent-hover": "#A5C8FA",
        "gm-accent-deep": "#4A90E2",
        "gm-success": "#34D399",
        "gm-warning": "#FBBF24",
        "gm-error": "#F87171",
      },
      fontFamily: {
        sans: ['"Google Sans"', "system-ui", "sans-serif"],
      },
      fontSize: {
        title: [
          "52px",
          { lineHeight: "1.1", fontWeight: "700", letterSpacing: "-0.02em" },
        ],
        h1: [
          "28px",
          { lineHeight: "1.2", fontWeight: "600", letterSpacing: "-0.01em" },
        ],
        h2: ["22px", { lineHeight: "1.25", fontWeight: "600" }],
        h3: [
          "15px",
          { lineHeight: "1.35", fontWeight: "600", letterSpacing: "-0.005em" },
        ],
        navbar: ["18px", { lineHeight: "1.4", fontWeight: "500" }],
        button: [
          "14px",
          { lineHeight: "1.4", fontWeight: "500", letterSpacing: "0.01em" },
        ],
        body: ["14px", { lineHeight: "1.6", fontWeight: "400" }],
        "body-lg": ["15px", { lineHeight: "1.6", fontWeight: "400" }],
        caption: [
          "12px",
          { lineHeight: "1.4", fontWeight: "500", letterSpacing: "0.02em" },
        ],
        "bento-body": ["16px", { lineHeight: "1.5", fontWeight: "400" }],
        overline: [
          "11px",
          { lineHeight: "1.2", fontWeight: "600", letterSpacing: "0.08em" },
        ],
      },
      spacing: {
        xs: "8px",
        sm: "12px",
        md: "16px",
        lg: "24px",
        xl: "32px",
        xxl: "48px",
        "2xxl": "64px",
      },
      borderRadius: {
        xs: "6px",
        sm: "8px",
        md: "12px",
        lg: "16px",
        xl: "20px",
        pill: "9999px",
      },
      backdropBlur: {
        glass: "24px",
        "glass-heavy": "40px",
      },
      boxShadow: {
        glass:
          "0 8px 32px rgba(0, 0, 0, 0.3), inset 0 0 0 1px rgba(255, 255, 255, 0.05)",
        "glass-hover":
          "0 12px 40px rgba(0, 0, 0, 0.4), inset 0 0 0 1px rgba(255, 255, 255, 0.08)",
        accent: "0 2px 12px rgba(138, 180, 248, 0.25)",
        "accent-lg": "0 4px 20px rgba(138, 180, 248, 0.35)",
        "accent-glow": "0 0 40px rgba(138, 180, 248, 0.2)",
        card: "0 1px 0 rgba(255, 255, 255, 0.04) inset, 0 12px 32px -12px rgba(0, 0, 0, 0.6)",
        focus: "0 0 0 3px rgba(138, 180, 248, 0.25)",
        "level-1":
          "0 1px 3px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.04)",
        "level-2":
          "0 4px 12px rgba(0, 0, 0, 0.1), 0 2px 4px rgba(0, 0, 0, 0.06)",
        "level-3":
          "0 12px 32px rgba(0, 0, 0, 0.12), 0 4px 8px rgba(0, 0, 0, 0.08)",
        "inner-subtle": "inset 0 1px 2px rgba(0, 0, 0, 0.06)",
        upload: "0 0 0 2px rgba(138, 180, 248, 0.4)",
      },
      backgroundImage: {
        "header-gradient": "linear-gradient(90deg, #8AB4F8 0%, #FFFFFF 100%)",
        "accent-gradient":
          "linear-gradient(135deg, #8AB4F8 0%, #A5C8FA 50%, #BDDCFF 100%)",
        "surface-gradient":
          "linear-gradient(180deg, rgba(255,255,255,0.02) 0%, rgba(255,255,255,0) 100%)",
        "radial-glow":
          "radial-gradient(ellipse at center, rgba(138, 180, 248, 0.15) 0%, transparent 70%)",
        "mesh-dark":
          "radial-gradient(at 40% 20%, rgba(138, 180, 248, 0.08) 0px, transparent 50%), radial-gradient(at 80% 80%, rgba(138, 180, 248, 0.05) 0px, transparent 50%)",
        "page-glow":
          "radial-gradient(1200px 520px at 15% -10%, rgba(138, 180, 248, 0.10), transparent 60%), radial-gradient(900px 480px at 100% 0%, rgba(165, 200, 250, 0.06), transparent 60%)",
        "primary-gradient": "linear-gradient(135deg, #8AB4F8 0%, #A5C8FA 100%)",
      },
      animation: {
        "fade-in": "fadeIn 0.4s ease-out",
        "fade-in-up": "fadeInUp 0.5s ease-out",
        "scale-in": "scaleIn 0.3s ease-out",
        "slide-up": "slideUp 0.4s cubic-bezier(0.16, 1, 0.3, 1)",
        "slide-down": "slideDown 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
        "pulse-soft": "pulseSoft 2s ease-in-out infinite",
        shimmer: "shimmer 2s linear infinite",
        "glow-pulse": "glowPulse 3s ease-in-out infinite",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        fadeInUp: {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        scaleIn: {
          "0%": { opacity: "0", transform: "scale(0.96)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(16px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        slideDown: {
          "0%": { opacity: "0", transform: "translateY(-8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.7" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        glowPulse: {
          "0%, 100%": { boxShadow: "0 0 20px rgba(138, 180, 248, 0.2)" },
          "50%": { boxShadow: "0 0 40px rgba(138, 180, 248, 0.4)" },
        },
      },
      transitionTimingFunction: {
        "out-expo": "cubic-bezier(0.16, 1, 0.3, 1)",
        "in-out-expo": "cubic-bezier(0.87, 0, 0.13, 1)",
      },
    },
  },
  plugins: [],
};
