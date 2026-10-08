## Running an investigation locally

Two ways to run the real investigation graph on your workstation:

1. `make standalone` runs AQuA and the dashboard against your deployed agent's
   telemetry, with storage in the agent process. See "Run AQuA standalone" in
   `docs/index.md`.
2. `uv run python tools/local_run.py` runs one investigation and prints its
   workflow events, with no dashboard. It writes to AQuA's BigQuery dataset
   unless `AQA_STANDALONE=1` is set, in which case storage is in memory.

`make standalone` reads `src/ambient_quality_agent/.env`; `local_run.py` reads
only the environment. `.env.example` at the repository root lists the settings.
For access to the observed agent's source code snapshot, also set
`AQA_SOURCE_GCS_BUCKET`.

## Running the dashboard on generated data

Visualization work needs a deployment with a history — ours has two
investigations and one failure, which is not enough to tell a chart that works
from one that happens to look right. `tools/mock_ui.sh` gives you a month of it
in about a second, with no cloud, no credentials and no model:

```
tools/mock_ui.sh                  # http://localhost:8080
```

It runs three pieces, each useful on its own:

| Script | What it does |
|---|---|
| `mock_aqa_data.py` | writes a generated deployment to JSON — sweeps on an ambient cadence, their stage counters, and the insights and occurrences they produced |
| `mock_aqa_server.py` | serves that file over the agent's **command and A2A routes**, so the dashboard's existing local-agent backend talks to it unchanged |
| `mock_ui.sh` | both of the above, then hands off to `local_ui.sh`, which finds the server already listening and starts only the UI |

**There is no agent, on purpose.** Every dashboard action is an HTTP command
route that the agent answers by calling one tool and returning its payload, so
the surface the UI depends on is a lookup table. An ADK agent in
front of that would add a Gemini round-trip, cloud credentials and a startup
minute to a dictionary lookup, and make the fixture non-deterministic besides.
What the mock *does* imitate exactly is the wire: `AdkHttpClient` and the
chat's A2A client run for real, so nothing in the UI is mock-aware.

Fidelity, where it matters:

- every record is built as the agent's own model and serialized by the agent's
  own presenter (`format_run`, `InsightView`, `InsightOccurrence`), so the
  payloads are not copies of the real shapes — they are the real shapes, and a
  model change breaks the generator rather than yielding a fixture that lies;
- the counters hold the funnel relations the nodes produce (sampled = ingested +
  partial + dropped, evaluated = ingested + partial, passed + failed + errored =
  evaluated, new + recurring = clusters), so a flow chart drawn from them
  conserves what it should;
- the investigations list is capped at 50 runs and the totals are summed over
  *every* run, exactly as `InvestigationStore.list_recent` and `.totals` do — so
  a period-scoped chart meets the same ceiling here as it will in production;
- `tools/test_mock_aqa.py` pins all of that, plus the A2A framing.

The generated month is deliberately not uniform, because uniform data hides
bugs: traffic follows a working week and a working day, the per-metric budget
bites during peak windows (so "traces in window" and "ingested" genuinely
differ), a regression lands three days before the end (failure rate ~8% → ~30%,
with three brand-new insights), two early defects were fixed and their insights
have aged into `RESOLVED`, one sweep is still `running`, one `failed`, one was
`skipped` as a duplicate trigger, and one lost rubrics to a clustering error.

```
tools/mock_aqa_data.py --days 60 --runs-per-day 8 --seed 3   # a bigger deployment
tools/mock_aqa_data.py --incident-days-ago 10                # move the regression
MOCK_KEEP=1 tools/mock_ui.sh                                 # reuse the last file
```

Two knobs generate a **degenerate** deployment instead, and they matter more
than they look: a healthy customer's account has nothing in it, the generated
month always has plenty, and a view that only works against the busy one breaks
where nobody is watching.

```
tools/mock_aqa_data.py --failure-rate 0    # nothing fails: no clusters, no insights
tools/mock_aqa_data.py --drop-rate 1       # nothing survives ingestion
MOCK_FAILURE_RATE=0 tools/mock_ui.sh       # the same two, through the dashboard
MOCK_DROP_RATE=1    tools/mock_ui.sh
```

`--failure-rate` overrides the baseline *and* the incident spike *and* the
per-sweep jitter, so 0 really means none: a knob that still produced the odd
failure would not be worth having.

The history is anchored at generation time, so "the last 7 days" is always the
7 days before you look — which is why `mock_ui.sh` regenerates on every start
and the server warns when it is handed a stale file.

## Inspecting what a sweep fed the clustering model

`tools/local_run.py --dump-dir scratch/<label>` runs the investigation graph and
writes `finding_sets.json` (one set per fetched page, before merging),
`findings.json` (the merged findings, in the order clustering indexes them), and
every session-review prompt and response under `reviews/`. Diffing two labels is
how a telemetry-source or prompt change gets compared.

## Load-testing clustering and correlation

`tools/insights_load_test.py` runs the insight pipeline's two LLM stages on a
corpus it generates itself, so a run needs no telemetry, no evaluation and no
observed agent — and its size is a flag:

```
GOOGLE_CLOUD_PROJECT=my-project \
    uv run --frozen python tools/insights_load_test.py --sizes 250,1000,5000
```

The corpus is eval pages minted round-robin from a fixed catalog of defects, so
every rubric's true cluster is known and the run can score the model's grouping,
not just whether it answered. Pages rather than rubric tuples, because the run
then goes through the node's own sequence — `extract_failed_rubrics`,
`cluster_and_label`, `validate_clusters`, `attach_examples`, `merge_clusters` —
and inherits the ids extraction mints. A corpus that built tuples directly would
bring its own id scheme, and id length is one of the things being measured.

`--fake-model` answers from the ground truth instead of calling Gemini: it
checks that plumbing in seconds and measures nothing about the model, so its
rows are marked `fake`.

`cluster_and_label` splits a sweep into `clustering.CHUNK_SIZE`-rubric chunks and
clusters them `CHUNK_CONCURRENCY` at a time (each retried up to `CHUNK_ATTEMPTS`),
so a response only ever has to echo back its own chunk's ids — the thing that
used to truncate a whole sweep. What that costs is deduplication: a chunk cannot
see the others, so one defect comes back once per chunk under its own wording,
and `merge_clusters` (in `insights/merge.py`) folds those back with one small
labels-only call. The report's `chunks` column is the calls a sweep took and
`pre` is the candidates they produced before the merge — `pre` above the defect
count is exactly what the merge exists to fold. Read `ids`/coverage rather than
the cluster count: a chunk lost to a `503` or classed `truncated` is thousands of
rubrics that reached no cluster while the survivors still look clean.

Correlation runs against real BigQuery: the clusters are written as insights
under a throwaway agent name and then re-matched against themselves. The store
is read with a plain `SELECT` and the judging is `generateContent`, so the cost
is `|candidates| x ceil(|insights| / batch)` model calls at worst and one per
candidate at best. Every candidate must match, so anything less is a duplicate
insight in production. The throwaway rows are deleted afterwards unless you pass
`--keep`; `--no-correlate` skips the stage and its BigQuery prerequisites
entirely.
