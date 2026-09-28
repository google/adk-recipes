# lha-web

Vite + React 18 chat UI for the `horizon` agent. Connects over A2A and renders the agent's streamed responses (text, tool calls, file artifacts) inline alongside live session telemetry.

## Stack

Vite + React 18 + TypeScript + Tailwind 3 + TanStack Router + TanStack Query + `@a2a-js/sdk`.

## What's in the UI

- **Chat column** — streaming text, tool rows, and file artifacts from the agent. HTML artifacts render inline in a sandboxed iframe.
- **Left rail** — Skills (load counts + failures), Todos (live status icons), Iteration (tool calls / iteration, halt state).
- **Right rail** — Memory (6 layers as tabs), Delegation tree.
- **Header** — connection state.
- **Guardrail banner** — green when armed, red when `halt_reason` fires.

Side panels read agent state via TanStack Query hooks (see Data); Query dedupes so each panel doesn't generate its own request.

## Run locally

The recommended path is from the repo root:

```bash
make dev        # backend on :8001 + web on :3000
```

To run just the UI against an already-running backend:

```bash
cp .env.local.example .env.local   # NEXT_PUBLIC_LHA_URL=http://127.0.0.1:8001
npm install
npm run dev                        # vite, http://localhost:3000
```

In dev, the proxy in `vite.config.ts` forwards `/lha`, `/a2a`, `/feedback`, and `/.well-known` → `${NEXT_PUBLIC_LHA_URL}` (default `http://127.0.0.1:8001`). Frontend calls are same-origin (e.g. `fetch("/lha/state")`).

## Routing

File-based TanStack Router routes in `src/routes/`:

- `index.tsx` — `/`
- `c.tsx` — `/c`, with a typed `?id=` search param (`validateSearch`)
- `__root.tsx` — root route / shell

The generated route tree is `src/routeTree.gen.ts`. Entry point is `src/main.tsx` mounting into `index.html` (`#root`).

## Data

Server state is fetched through TanStack Query hooks in `lib/`:

- `useLhaState` (`lib/horizon-state.ts`)
- `useLhaSessions` (`lib/horizon-sessions.ts`)
- `useLhaTasks` (`lib/horizon-tasks.ts`)
- `useLhaMemories` (`lib/horizon-memories.ts`)
- `useLhaSecrets` (`lib/horizon-secrets.ts`)
- `useSandboxStatus` (`lib/horizon-sandbox-status.ts`)

The `QueryClient` is built in `lib/query-client.ts` and the query-key factory lives in `lib/query-keys.ts` (`qk`). A2A streaming is handled separately from Query via `@a2a-js/sdk` (`lib/a2a-client.ts`).

## Build & serve

```bash
npm run build      # vite build → web/dist/
```

In production the `dist/` bundle is served by the `web/server/` Express proxy, which also proxies `/lha`, `/a2a`, `/feedback`, and `/.well-known` to the FastAPI backend and validates the IAP JWT (`web/server/server.js`).

## Tests

```bash
npm run typecheck   # tsc --noEmit
npm run test:unit   # vitest
npm run test:e2e    # playwright (chromium)
```

## Backend touchpoints

The relevant backend wiring lives in:

- `app/a2a/routes.py` — builds the agent card and registers the A2A JSON-RPC routes.
- `app/a2a/executor.py` — the A2A executor: per-session task tracking (`lha:task_ids` / `lha:active_task_id`) and `adk_partial` event tagging.
- `app/api/state.py` — read-only `GET /lha/state` for the side panels.
- `app/agent.py` — the `root_agent` instruction nudges the model to save rich/visual output as a self-contained HTML artifact.

## Showcase prompts

| Prompt | What lights up |
|---|---|
| "What skills do you have?" | Skills panel counters bump |
| "Fetch a public dataset, plot it, save an HTML dashboard" | HTML artifact renders inline in a sandboxed iframe |
| Trigger a guardrail (e.g. tight loop) | Red `guardrail · halt` banner with last error |

## Layout

```
web/
├── index.html                    # Vite entry document (#root)
├── vite.config.ts                # build (→ dist/) + dev proxy
├── src/
│   ├── main.tsx                  # app entry — mounts router
│   ├── router.tsx                # TanStack Router setup
│   ├── routeTree.gen.ts          # generated route tree
│   ├── fonts.css                 # @font-face (woff2 in public/fonts) + --font-geist-* vars
│   └── routes/                   # file-based routes (__root, index, c)
├── app/
│   └── globals.css               # shadcn tokens + lh-* brand vars
├── components/
│   ├── chat/                     # chat shell + message list/bubble + input
│   ├── panels/                   # side panels (read the TanStack Query hooks)
│   └── ui/                       # shadcn primitives
├── lib/                          # TanStack Query hooks, query-client/keys, A2A client, helpers
└── public/fonts/                 # Geist woff2s referenced by src/fonts.css
```
