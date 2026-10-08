# The dashboard as an `agents-cli` project

This directory makes the AQA dashboard an ordinary `agents-cli` Cloud Run
project, so that `agents-cli deploy` builds and pushes its image the way it does
for any other project, instead of [`deploy_ui_cloud_run.sh`](deploy_ui_cloud_run.sh) doing it by
hand. Tracking bug: b/550103956.

`agents-cli deploy --deploy-aqua` deploys from here, as the third step the AQuA
extension adds, and `--skip-aqua-ui` leaves it out. That step is
[`extension/acli_ui.py`](../extension/acli_ui.py), whose module docstring is the
design document for it: where it sits in the chain, how it assembles a build
context, and why it updates the Cloud Run service Terraform created rather than
creating one of its own.

```
ui/
├── agents-cli-manifest.yaml   deployment_target: cloud_run, language: python
├── Dockerfile
├── requirements.txt
├── .dockerignore
├── .gcloudignore              the build context, which is this directory
├── deploy_ui_cloud_run.sh     the deploy without agents-cli; not in the image
└── web/                       the front end: React + Vite + Tailwind + shadcn
```

`npm run build` writes the bundle to `../static_v2` — beside this file, not
inside `web/` — which is why the root `.gitignore` has an entry for it. The
Dockerfile's `node:22-slim` stage builds it during the image build, so the
bundle in the image is always the one that matches the source beside it, never
whatever was last compiled on someone's laptop.

It is what `/` serves, and the only thing `/` serves: with no bundle the route
answers 503 with the build command rather than falling back to anything.

`make demo` is the shortest way to look at it: it installs, builds and serves
this front end over a generated month of data, with no project and no
deployment.

The FastAPI service itself is `app.py`, and the front end it serves is
documented in [`web/README.md`](web/README.md); [`DASHBOARD.md`](DASHBOARD.md)
covers the service — its endpoints, its environment and how a click reaches the
agent. The one thing still outside this directory is `ambient_quality_shared`,
the wire client shared with `src/ambient_quality_cli`, which `app.py` imports
at module scope and which
`--source .` from here cannot reach. So neither deploy path builds straight from
this directory: both assemble a context out of this one and
`src/ambient_quality_shared`. `acli_ui.py` explains the choice; the Dockerfile's
guard fails a build whose context is missing the wire client, rather than
producing an image that starts and immediately dies.

## What is still open

- **`ambient_quality_shared` is copied, not depended on.** The wire client the
  dashboard shares with `src/ambient_quality_cli` is not a distribution — it is
  staged into the build context. Making it a real dependency is the clean answer
  and the most work, and it is what would let the staging go away entirely.
- **Whether this project gets its own Terraform root.** `acli_infra.py` runs two
  roots today; a `deployment/terraform/single-project/` here would be a third.
  It is not obviously right — the service's environment references the AQuA
  engine, which lives in the other root — so it waits on the ownership question
  above.
- **Two cards say "not yet" and will until something publishes.** `/api/source`
  and `/api/investigations/{run}/artifact` answer `available: false` with a
  reason the cards render: no source snapshot is published at deploy time, and
  no phase writes a per-run artifact. Both are agent-side already, so the writer
  is the only thing left to add.
- **Two dead exports survive in `web/`** — `useScheduledSessions`, and
  `setChatWindow` behind a guard nothing populates. Both have tests, so removing
  them is a code change with its own review rather than a documentation one.

The full proposal is in the tracking bug (comment #7).
