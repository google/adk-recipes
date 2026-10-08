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

import type { Config } from "tailwindcss";
import plugin from "tailwindcss/plugin";
import tailwindcssAnimate from "tailwindcss-animate";
import typography from "@tailwindcss/typography";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{ts,tsx}",
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    container: { center: true, padding: "2rem" },
    // Replaced, not extended: anything off the scale does not exist. Text sets
    // these through the roles in lib/typography.ts.
    fontSize: {
      xs: ["0.75rem", { lineHeight: "1rem" }],
      sm: ["0.875rem", { lineHeight: "1.25rem" }],
      base: ["1rem", { lineHeight: "1.5rem" }],
      xl: ["1.25rem", { lineHeight: "1.75rem" }],
    },
    fontWeight: { normal: "400", medium: "500" },
    letterSpacing: {},
    fontFamily: {
      sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
      mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
    },
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // Brand semantic tokens — used by chat bubbles, panels, surfaces
        lh: {
          weft: "hsl(var(--lh-weft))",
          warp: "hsl(var(--lh-warp))",
          thread: "hsl(var(--lh-thread))",
          shuttle: "hsl(var(--lh-shuttle))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        "fade-in-up": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        // The dwell until Run investigation re-reads the list. The duration is
        // set inline from POLL_INTERVAL_MS rather than stated here, so the bar
        // cannot drift from the poll it depicts.
        "run-dwell": {
          from: { width: "0%" },
          to: { width: "100%" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        shimmer: "shimmer 2.4s linear infinite",
        "fade-in-up": "fade-in-up 0.18s ease-out",
      },
      // Markdown, mapped onto the type scale. Every place that renders
      // markdown goes through components/chat/markdown.tsx. Prose colors follow
      // the app's tokens, which flip under `.dark`, so no `prose-invert` is
      // needed.
      typography: {
        DEFAULT: {
          css: {
            "--tw-prose-body": "hsl(var(--foreground))",
            "--tw-prose-headings": "hsl(var(--foreground))",
            "--tw-prose-lead": "hsl(var(--foreground))",
            "--tw-prose-bold": "hsl(var(--foreground))",
            "--tw-prose-counters": "hsl(var(--muted-foreground))",
            "--tw-prose-bullets": "hsl(var(--muted-foreground))",
            "--tw-prose-links": "hsl(var(--primary))",
            "--tw-prose-code": "hsl(var(--lh-code))",
            "--tw-prose-quotes": "hsl(var(--muted-foreground))",
            "--tw-prose-quote-borders": "hsl(var(--border))",
            "--tw-prose-captions": "hsl(var(--muted-foreground))",
            "--tw-prose-hr": "hsl(var(--border))",
            "--tw-prose-th-borders": "hsl(var(--border))",
            "--tw-prose-td-borders": "hsl(var(--border))",
            // The two weights: 500 for headings and emphasis, 400 for the rest.
            "h1, h2, h3, h4, h5, h6, strong, dt, thead th": {
              fontWeight: "500",
            },
            "h1 strong, h2 strong, h3 strong, h4 strong": {
              fontWeight: "inherit",
            },
            a: { fontWeight: "inherit" },
            code: { fontWeight: "400" },
            // Inline code is a chip; code blocks render through CodeBlock.
            ":not(pre) > code": {
              color: "var(--tw-prose-code)",
              backgroundColor:
                "color-mix(in srgb, var(--tw-prose-code) 12%, transparent)",
              border:
                "1px solid color-mix(in srgb, var(--tw-prose-code) 18%, transparent)",
              borderRadius: "0.375rem",
              padding: "0.08em 0.34em",
              overflowWrap: "anywhere",
            },
            "code::before": { content: "none" },
            "code::after": { content: "none" },
          },
        },
        sm: {
          css: {
            p: {
              marginTop: "0.375rem",
              marginBottom: "0.375rem",
              lineHeight: "1.625",
            },
            "h1, h2": { fontSize: "1rem", lineHeight: "1.5rem" },
            "h3, h4, h5, h6": { fontSize: "0.875rem", lineHeight: "1.25rem" },
            "h1, h2, h3, h4, h5, h6": {
              marginTop: "0.75rem",
              marginBottom: "0.5rem",
            },
            "ul, ol": { marginTop: "0.375rem", marginBottom: "0.375rem" },
            li: { marginTop: "0.125rem", marginBottom: "0.125rem" },
            // 12px in 14px text: the code role's size.
            code: { fontSize: "0.8571429em" },
            table: { marginTop: "0.5rem", marginBottom: "0.5rem" },
            th: {
              padding: "0.25rem 0.5rem",
              backgroundColor: "hsl(var(--muted) / 0.4)",
            },
            td: { padding: "0.25rem 0.5rem" },
          },
        },
        // The user's own bubble sits on a primary fill, so every prose color,
        // inline code included, follows that fill's foreground.
        "on-primary": {
          css: {
            "--tw-prose-body": "hsl(var(--primary-foreground))",
            "--tw-prose-headings": "hsl(var(--primary-foreground))",
            "--tw-prose-bold": "hsl(var(--primary-foreground))",
            "--tw-prose-links": "hsl(var(--primary-foreground))",
            "--tw-prose-code": "hsl(var(--primary-foreground))",
            "--tw-prose-bullets": "hsl(var(--primary-foreground))",
            "--tw-prose-counters": "hsl(var(--primary-foreground))",
          },
        },
      },
    },
  },
  plugins: [
    tailwindcssAnimate,
    typography,
    // Google Sans has two designs on its optical-size axis: Google Sans Text
    // (17), drawn for small sizes, and Google Sans (18), for titles. These
    // utilities pick one explicitly, because browsers do not all apply optical
    // sizing on their own.
    plugin(({ addUtilities }) =>
      addUtilities({
        ".opsz-text": { fontVariationSettings: '"opsz" 17' },
        ".opsz-display": { fontVariationSettings: '"opsz" 18' },
      }),
    ),
  ],
};

export default config;
