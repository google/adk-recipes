# The AQuA dashboard front end

Vite + React 18 single-page app: the investigations and insights dashboard, the
chat panel, and the configuration surfaces. Served by the FastAPI app in
[`app.py`](../app.py) beside this directory, same-origin, as a static bundle.

Several things below are that way on purpose rather than by accident — the
two test runners and the loud stubs. Each is called out where it applies,
because each one gets undone by someone tidying up otherwise.

## Stack

React 18 + TypeScript + Vite + Tailwind 3 + shadcn/Radix + TanStack
Router/Query + `@a2a-js/sdk`.

**`@/` points at this directory**, mapped in `tsconfig.json` as `@/* → ./*` and
mirrored in `vite.config.ts`.

**`package.json` and `package-lock.json` are the record of dependency
versions.** Read them, or run `npm ls <package>`, rather than copying versions
into documentation, where they go stale.

## Routes

File-based TanStack Router routes in `src/routes/`, with the generated tree in
`src/routeTree.gen.ts` — rewritten by the Vite plugin, not edited by hand.

| Route | Shows |
|---|---|
| `index.tsx` → `/` | the home dashboard and its ask bar |
| `c.tsx` → `/c` | the chat panel, with a typed `?id=` conversation param |
| `investigations.index.tsx` | the sweep list |
| `investigations.$runId.index.tsx` | one sweep: counters, step ledger, findings |
| `investigations.$runId.cases.$caseId.tsx` | one trajectory, replayed turn by turn |
| `insights.index.tsx` | the insight list, with merge and dismiss |
| `insights.$insightId.tsx` | one insight: occurrences, rubrics, permalink |
| `config.tsx` | the effective configuration, the goal and the memories |
| `__root.tsx` | the shell: nav rail, theme, query client |

## How it reaches the backend

Everything is same-origin; the app reads no runtime environment variable and
names no host.

| Prefix | For | State |
|---|---|---|
| `/api/*` | every dashboard read and write — `stats`, `daily`, `investigations`, `insights`, `config`, `goal`, `memories`, `source`, `health` | served today by `app.py` |
| `/a2a`, `/.well-known/agent-card.json` | the chat transport, proxied same-origin to the agent | served by `app.py` |
| `/lha/*` | `contexts`, `sessions`, `workspace` — the conversation list and the file surfaces | not implemented here; see below |

**A missing backend is stubbed loudly, never silently.** Nothing here serves
`/lha/*` or `/feedback` yet, so `app.py` answers them with a 501 and a worded
reason rather than an empty object — an empty list renders as "nothing here",
which is a lie, and this dashboard has been bitten by that four times already.
The client short-circuits on a 501 and would retry against anything else, so the
status matters as much as the body.

That answer comes from the SPA catch-all, which is the one thing that must never
serve HTML to a `fetch`: 200 plus a `<!doctype html>` reads as success and fails
somewhere unrelated. The prefixes are listed explicitly so that whichever change
adds the real routes cannot regress it by registering them in the wrong place.

`/lha/contexts` is the exception: a real endpoint over the chat task store.
`/feedback` and `/lha/sandbox/*` appear in the dev proxy's forward list but are
called from nowhere in this tree.

## Build

```bash
npm ci
npm run check     # tsc --noEmit, vitest run, Biome format check and Biome lint
npm run build     # → ../static_v2, not ./dist
```

`package.json` overrides `postcss-selector-parser` to 7.x for
GHSA-rj75-hqrm-r3gf, because tailwindcss 3 and `@tailwindcss/typography` still
ask for 6.x. Remove the override once both accept 7.x.

`vite.config.ts` sets `base: "/"` and `outDir: "../static_v2"`: the app is
served from the root of the UI service, and its bundle is written *beside* this
directory rather than inside it. The image is meant to build it in a Node stage;
the root `.gitignore` keeps a locally built copy out of the repository.

`components/insights/insight-detail.tsx` builds an insight's shareable
permalink as `${origin}/v2/insights/<id>`, which the app itself never routes —
the build has always been served from the root, whatever older documentation
claimed. Rather than change the permalink and break links already sent to
people, `app.py` answers `/v2/<anything>` with a 308 to `/<anything>`.

## Running it locally

Three ways in. The first needs nothing but a checkout:

```bash
make demo       # generated data, no GCP project, no deployment, no model
```

It installs, builds, and serves this app over a month of synthetic
investigations and insights.

The other two want a real backend, and the second of them is the one that
matches production.

**Vite dev server**, for working on the front end — hot reload, and the proxy
forwards `/api`, `/lha`, `/a2a` and `/.well-known` to a backend you start
separately:

```bash
tools/local_ui.sh &                 # the FastAPI app on :8080
npm run dev                         # the front end on :3000
```

**Through FastAPI**, which is how it is actually served — no hot reload, but the
same routing the image has, including the SPA fallback and the `/v2` redirect:

```bash
npm run build
tools/local_ui.sh                   # http://localhost:8080
```

A checkout needs `npm run build` first. There is nothing to fall back to, so
with no bundle `/` answers 503 with the build command and the log says the
same — the image builds one in its Node stage, and a deployment without one is
misbuilt.

## Configuration

`app-config.json` holds the values that are a property of the deployment rather
than of the code — specifically `feedbackUrl`, the issue tracker opened by the
nav bar's help button. It is imported, not fetched, so a change takes effect on
the next `npm run build` (or image build); nothing reads it at run time.

## Guided tour

The compass button in the tab bar walks a reader through the dashboard, one
feature at a time: a spotlight that glides to the feature and a callout that
says what it is for. When the next feature lives on another page, a pointer
clicks the link that leads there, so the reader learns the way. `?tour` in
the address opens it on load, so a link can hand someone the tour —
`make demo`, then `http://localhost:8080/?tour`, shows it on generated data.

The steps are in `components/tour/tour-steps.ts`. Each points at an element
marked `data-tour="<name>"`, and a step whose page needs an id out of the data
(an insight, an investigation) is skipped when the deployment has none.
A step whose element is gone still opens, centered and pointing at nothing, so
two tests guard against it. `components/tour/__tests__/tour-steps.test.ts`
fails when no component marks a step's element any more.
`components/tour/__tests__/tour-on-the-dashboard.test.tsx` opens every step
over the real routes and pages, on canned data, and fails when a step's page
no longer exists or no longer renders its element: a renamed route, a section
moved to another page, an anchor lost in a refactor. When you move or rename
something the tour points at, update `tour-steps.ts` with it.

## Tests

`npm run check` is the gate, and it is what CI runs (`frontend-tests.yml`),
followed by `npm run build` — `tsc` and vitest do not run Vite's transform, so
a build failure passes both.

`npm run lint` runs Biome with the same version, rules and
`--error-on-warnings` as adk-recipes CI, which lints the TypeScript and
JavaScript files that each change to the exported recipe touches, and allows no
recipe-local Biome config. A finding is fixed in the code, or suppressed with a
`biome-ignore` comment that gives the reason. The CSS linter is off, as the
CSS formatter is for `format`: Biome's CSS parser rejects Tailwind's `@apply`
unless a config option enables it, and adk-recipes does not lint CSS.

**The `e2e/` suite is not wired up.** All nine `*.spec.mjs` files sit in
Playwright's `testDir`, but not one declares a `test()`: they are plain Node
scripts that drive a live deployment at `AQUA_BASE_URL` (default
`localhost:8080`), while `playwright.config.ts` starts a dev server and points
`baseURL` at it. So `npm run test:e2e` collects nine files and runs nothing.
Converting them, or moving them out of `testDir`, is an open item.

## Unused exports

Two exports have no callers: `useScheduledSessions`,
and `setChatWindow` behind a `takePendingWindow` guard that nothing populates —
dead in effect, though live enough to a grep that `chat-shell.tsx` appears to
call the second. Both carry tests, so removing them is a code change with its
own review rather than a documentation one.

## Layout

```
web/
├── index.html              Vite entry document (#root)
├── app-config.json         deployment-specific values (the feedback URL)
├── vite.config.ts          build (→ ../static_v2), dev proxy, @/ alias
├── src/
│   ├── main.tsx            entry; mounts the router
│   ├── routeTree.gen.ts    generated — do not edit
│   ├── fonts.css           font family variables (index.html loads the fonts from Google Fonts)
│   └── routes/             the nine routes above
├── app/globals.css         shadcn tokens and brand variables
├── components/
│   ├── chat/               the chat panel, artifacts, command palette
│   ├── conversation/       evidence-case replay
│   ├── insights/           list, detail, merge bar
│   ├── investigations/     list, detail, step ledger
│   ├── trajectory/         the trajectory viewer
│   ├── home/  config/  nav/  brand/
│   └── ui/                 shadcn primitives
├── lib/                    TanStack Query hooks, the A2A client, helpers
└── public/                 favicons, copied to the bundle root
```
